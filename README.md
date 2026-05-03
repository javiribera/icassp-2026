# ICASSP 2026 Proceedings Scraper

Fetches metadata (title, authors, DOI, abstract, keywords) for all papers in the
[ICASSP 2026 proceedings](https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding)
on IEEE Xplore (4 589 papers).

## Output

| File | Description |
|------|-------------|
| `output/papers.json` | Raw API response records (one object per paper) |
| `output/papers.csv` | Flat table: title, authors, DOI, abstract, keywords, URL |

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
pip install -r requirements.txt
# For browser mode only:
# pip install playwright && playwright install chromium
```

## Docker (REST API mode)

```bash
docker build -t icassp-scraper .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper
# With cookie:
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper \
  --cookie "JSESSIONID=abc123; ..."
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--browser` | off | Use Playwright instead of the REST API |
| `--cookie STRING` | — | Session cookie (REST API mode only) |
| `--delay SECONDS` | 1.5 | Pause between requests |
| `--output DIR` | `./output` | Output directory |

## Notes

- Respect IEEE Xplore's [Terms of Use](https://ieeexplore.ieee.org/Xplorehelp/overview-of-ieee-xplore/terms-of-use).
  The default 1.5 s delay keeps request rates polite.
- If the scraper returns 0 records, the internal API path may have changed;
  inspect XHR traffic on the proceedings page to find the updated URL/params.
