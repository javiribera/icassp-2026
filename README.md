# ICASSP 2026 Proceedings Scraper

Fetches paper metadata (title, authors, DOI, abstract, keywords) from the
[ICASSP 2026 proceedings](https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding)
on IEEE Xplore.

The scraper calls the internal REST API that the IEEE Xplore SPA uses, so no
headless browser is needed.

## Output

| File | Description |
|------|-------------|
| `output/papers.json` | Raw API response records |
| `output/papers.csv` | Flattened table (title, authors, DOI, abstract, keywords, URL, …) |

## Usage

### Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scraper.py                     # writes to ./output/
python scraper.py --output data/      # custom output dir
python scraper.py --delay 2.0         # slower crawl (default 1.5 s)
```

### Docker

```bash
docker build -t icassp-scraper .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper
# pass extra flags after the image name:
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper --delay 2.0
```

## Notes

- IEEE Xplore does not require authentication for public conference metadata.
- If the scraper returns 0 records, the internal API endpoint may have changed;
  inspect XHR traffic on the proceedings page to find the updated URL/params.
- Respect IEEE Xplore's [Terms of Use](https://ieeexplore.ieee.org/Xplorehelp/overview-of-ieee-xplore/terms-of-use).
  The default 1.5 s delay keeps request rates well within polite limits.
