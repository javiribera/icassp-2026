# ICASSP 2026 Proceedings Scraper

Fetches metadata for all 4,589 papers in the
[ICASSP 2026 proceedings](https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding)
on IEEE Xplore, then scores the relevance of every paper from 0 to 100 using a
custom prompt that describes your research interests.

## How to Use

### Step 1 - scrape all papers

```bash
docker build -t icassp-scraper .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper
```

Output is written to `output/papers.json` (full metadata) and `output/papers.csv`
(title, abstract, url only).

If you get HTTP 403, copy your browser session cookie and pass it:

1. Open the proceedings page in Chrome.
2. Open DevTools → Network → filter by `rest/search`.
3. Click "Load More" → select the request → copy the full `Cookie:` header value.
4. Run:

```bash
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper \
  --cookie "JSESSIONID=abc123; TS01...=..."
```

### Step 2 - score relevance (optional)

Get an API key at [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key.

Edit `PROMPT_FOR_RELEVANCE.txt` to describe who you are and what papers you care about.

Run:

```bash
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/PROMPT_FOR_RELEVANCE.txt:/app/PROMPT_FOR_RELEVANCE.txt:ro" \
  -e ANTHROPIC_API_KEY=sk-... \
  --entrypoint python icassp-scraper \
  estimate_relevance.py
```

Reads `output/papers.json` and produces `output/papers_with_relevance.json` and
`output/papers_with_relevance.csv`, each with a new `relevance` field (0–100).
Interrupted runs resume automatically.


## How it works

The proceedings page is a JavaScript SPA. When you click "Load More", no new
HTML page loads — the button fires a `fetch()` call to the server's internal
REST endpoint and appends the JSON results to the DOM. The scraper calls that
same endpoint directly in a loop, bypassing the button entirely.

### What has been verified

The internal IEEE Xplore REST API is undocumented but has been independently
observed and used by multiple public projects. Cross-referencing
[ieee_journal_downloader](https://github.com/FongYoong/ieee_journal_downloader),
[RSSHub discussions](https://github.com/DIYgod/RSSHub/discussions/8571), and
other scrapers confirms the following:

| Claim | Status |
|-------|--------|
| Base endpoint `ieeexplore.ieee.org/rest/search` | ✅ Confirmed — **POST with JSON body** (GET returns 405) |
| `rowsPerPage` parameter (max 100) | ✅ Confirmed |
| Response field `records` (array) | ✅ Confirmed |
| Response field `totalRecords` | ✅ Confirmed |
| Response field `articleTitle` | ✅ Confirmed |
| Response field `articleNumber` | ✅ Confirmed |
| Response field `doi` | ✅ Confirmed |
| Response field `abstract` | ✅ Confirmed — **but truncated** (see below) |
| `authors[].preferredName` | ✅ Confirmed |
| `authors[].affiliation` | ✅ Confirmed |
| Document endpoint `/rest/document/{id}/` | ✅ Confirmed |
| `newsearch=true` parameter | ✅ Confirmed — appears in live IEEE Xplore URLs and search results |
| `pageNumber` parameter | ✅ Confirmed — used in live IEEE Xplore URLs and independently in [1PageConference](https://github.com/ResearchGear/1PageConference/blob/master/ieee.py) |
| `punumber` body field for `/rest/search` | ✅ Confirmed — `publication-number` in the POST body was silently ignored (returned all 7M IEEE papers); switching to `punumber` correctly filters to the conference |

### Abstract truncation

Abstracts returned by the `/rest/search` endpoint are **truncated**
(confirmed by [RSSHub discussion #8571](https://github.com/DIYgod/RSSHub/discussions/8571)).
The full abstract is only available from the per-paper document endpoint
(`/rest/document/{articleNumber}/`). The scraper automatically fetches the
full abstract during the affiliation enrichment phase. Pass `--no-affiliations`
only if truncated abstracts are acceptable.

## Scraping phases

The scraper runs in two sequential phases:

| Phase | Requests | What it fetches |
|-------|----------|-----------------|
| Search | 46 calls (100 papers/page) | title, truncated abstract, authors, DOI, article number |
| Detail enrichment | ~4,589 calls (1 per paper) | full abstract, per-author institution strings |

Affiliations are absent from the search results and require a separate
`GET /rest/document/{articleNumber}/` call per paper. Skip this phase with
`--no-affiliations`.

## Output

`output/papers.json` — a JSON array, one object per paper:

```json
[
  {
    "title": "...",
    "venue": "ICASSP 2026 - 2026 IEEE International Conference on Acoustics, Speech and Signal Processing (ICASSP)",
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

`output/papers.csv` — a CSV file with three columns: `title`, `abstract`, `url`.


## All options

**scraper.py**

| Flag | Default | Description |
|------|---------|-------------|
| `--cookie STRING` | — | Full `Cookie:` header value from browser DevTools |
| `--delay SECONDS` | 1.5 | Pause between requests |
| `--workers N` | 8 | Concurrent workers for detail fetching |
| `--no-details`, `--no-affiliations` | off | Skip detail fetching; affiliations will be empty and abstracts may be truncated |
| `--limit N` | — | Fetch only the first N papers (useful for testing; disables checkpointing) |
| `--output DIR` | `./output` | Directory for `papers.json` and `papers.csv` |

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

A checkpoint is saved to `<output>/checkpoint.json` after the search phase
and every 200 papers during detail fetching. If the run is interrupted for
any reason, re-run the same command with the same `--output` directory and
it will pick up where it left off. The checkpoint is deleted automatically
on successful completion.

Checkpointing is disabled when `--limit` is used.


## Caveats

- The internal IEEE Xplore API is undocumented and may change without notice.
  If the scraper returns 0 results, inspect XHR traffic in DevTools to find
  the updated endpoint or parameters.
- Respect IEEE Xplore's [Terms of Use](https://ieeexplore.ieee.org/Xplorehelp/overview-of-ieee-xplore/terms-of-use).
  The default 1.5 s delay keeps request rates polite.
