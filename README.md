# ICASSP / CVPR / ICCV / WACV Proceedings Scraper

Scrapes paper metadata from academic conference proceedings, then scores the
relevance of every paper from 0 to 100 using a custom prompt that describes
your research interests.

**Supported conferences**

| Conference | Source | Notes |
|------------|--------|-------|
| `icassp` | IEEE Xplore internal REST API | Requires `--year 2026` (or add more years to `IEEE_PUBNUMS`) |
| `cvpr`, `iccv`, `wacv` | [openaccess.thecvf.com](https://openaccess.thecvf.com) static HTML | Any past year; CVPR2026 available ~Jun 2026 |

## How to Use

### Step 1 — scrape papers

```bash
# CVPR 2024 (CVF source)
docker build -t conf-scraper .
docker run --rm -v "$(pwd)/output:/app/output" conf-scraper \
  --conference cvpr --year 2024

# ICASSP 2026 (IEEE Xplore source)
docker run --rm -v "$(pwd)/output:/app/output" conf-scraper \
  --conference icassp --year 2026

# If ICASSP returns HTTP 403, pass a browser session cookie:
docker run --rm -v "$(pwd)/output:/app/output" conf-scraper \
  --conference icassp --year 2026 \
  --cookie "JSESSIONID=abc123; TS01...=..."
```

Output is written to `output/papers.json` (full metadata) and `output/papers.csv`
(title, abstract, url only).

### Step 2 — score relevance (optional)

Get an API key at [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key.

Edit `PROMPT_FOR_RELEVANCE.txt` to describe who you are and what papers you care about.

```bash
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/PROMPT_FOR_RELEVANCE.txt:/app/PROMPT_FOR_RELEVANCE.txt:ro" \
  -e ANTHROPIC_API_KEY=sk-... \
  --entrypoint python conf-scraper \
  estimate_relevance.py
```

Reads `output/papers.json` and produces `output/papers_with_relevance.json` and
`output/papers_with_relevance.csv`, each with a new `relevance` field (0–100).
Interrupted runs resume automatically.


## Output schema

`output/papers.json` — a JSON array, one object per paper:

```json
[
  {
    "title": "...",
    "venue": "CVPR 2024",
    "year": 2024,
    "abstract": "...",
    "authors": [
      {"name": "Alice Smith", "affiliation": "MIT, Cambridge, MA, USA"},
      {"name": "Bob Jones",   "affiliation": ""}
    ],
    "doi": "10.1109/...",
    "url": "https://openaccess.thecvf.com/content/CVPR2024/html/...",
    "pdf_url": "https://openaccess.thecvf.com/content/CVPR2024/papers/..."
  }
]
```

Field notes:
- `year` — integer from `--year`
- `affiliation` — populated for IEEE; always `""` for CVF (site lists names only)
- `doi` — populated for IEEE; `""` for CVF
- `pdf_url` — populated for CVF; `""` for IEEE

`output/papers.csv` — columns: `title`, `abstract`, `url`


## How it works

### CVF backend (cvpr / iccv / wacv)

The CVF Open Access site is static HTML. The scraper:
1. **Listing phase:** fetches `https://openaccess.thecvf.com/{CONF}{year}?day=all` and
   parses `<dt class="ptitle"><a href="...">Title</a></dt>` entries. Falls back to
   per-day pages if `?day=all` returns nothing.
2. **Detail phase:** fetches each per-paper page to get the full abstract, author list
   (`<div id="abstract">`, `<div id="authors">`), and PDF URL.

No authentication required. CVPR2026 will return nothing until papers are published
(~June 2026); test with `--conference cvpr --year 2024`.

### IEEE backend (icassp)

Calls the internal XHR endpoint used by the IEEE Xplore SPA
(`POST /rest/search` + `GET /rest/document/{articleNumber}/`).
If the endpoint returns 403, pass `--cookie` with a session cookie from DevTools.

See [README — abstract truncation note](https://github.com/DIYgod/RSSHub/discussions/8571):
the `/rest/search` response truncates abstracts; the detail phase fetches full text.


## Scraping phases

| Phase | What it fetches |
|-------|-----------------|
| Listing | title, venue, year, URL (and PDF URL for CVF) |
| Detail enrichment | full abstract, authors, affiliations (IEEE), PDF URL (CVF) |

Skip detail enrichment with `--no-details` (faster; abstracts/affiliations may be empty).


## All options

**scraper.py**

| Flag | Default | Description |
|------|---------|-------------|
| `--conference CONF` | *(required)* | `icassp` \| `cvpr` \| `iccv` \| `wacv` |
| `--year YEAR` | *(required)* | Conference year (e.g. 2026) |
| `--cookie STRING` | — | Full `Cookie:` header value from browser DevTools (IEEE only) |
| `--delay SECONDS` | 1.5 | Pause between requests |
| `--workers N` | 8 | Concurrent workers for detail fetching |
| `--no-details`, `--no-affiliations` | off | Skip detail phase |
| `--limit N` | — | Fetch only the first N papers (for testing; disables checkpointing) |
| `--output DIR` | `./output` | Output directory |

**estimate_relevance.py**

| Flag | Default | Description |
|------|---------|-------------|
| `--input PATH` | `output/papers.json` | Input file |
| `--output DIR` | same dir as input | Output directory |
| `--model ID` | `claude-haiku-4-5` | Claude model to use |
| `--no-batch` | off | Use real-time concurrent calls instead of the Batches API |
| `--workers N` | `8` | Concurrent workers (only with `--no-batch`) |
| `--limit N` | — | Score only the first N papers (use with `--no-batch` for testing) |


## Checkpoint / resume

A checkpoint is saved to `<output>/checkpoint.json` after the listing phase and
every 200 detail fetches. If interrupted, re-run with the same `--output` directory
to resume. Deleted automatically on successful completion.

Checkpointing is disabled when `--limit` is used.

`estimate_relevance.py` saves a separate `<output>/relevance_checkpoint.json`
(or the batch ID, when using the Batches API). Resume works the same way.


## Caveats

- The IEEE Xplore internal API is undocumented and may change without notice.
  If it returns 0 results, inspect XHR traffic in DevTools to find updated parameters.
- Respect IEEE Xplore's [Terms of Use](https://ieeexplore.ieee.org/Xplorehelp/overview-of-ieee-xplore/terms-of-use).
  The default 1.5 s delay keeps request rates polite.
- CVF abstracts and authors are only available on individual paper pages (phase 2).
