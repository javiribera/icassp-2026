#!/usr/bin/env python3
"""
Scrapes paper metadata from the ICASSP 2026 proceedings on IEEE Xplore.
https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding

Strategy
--------
1. REST API mode (default, fast):
   Calls the internal XHR endpoint used by the IEEE Xplore SPA.
   If the endpoint returns 403, pass --cookie with a session cookie copied
   from your browser (DevTools → Network → any XHR → Request Headers → Cookie).

2. Browser mode (--browser, slower but always works):
   Drives a real Chromium instance via Playwright, reads the same XHR
   responses by intercepting network traffic.
   Requires:  pip install playwright && playwright install chromium

Output: output/papers.json  — array of objects with keys:
          title, abstract, authors (list), doi, url
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests
from tqdm import tqdm

CONF_URL = "https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding"
API_BASE = "https://ieeexplore.ieee.org/rest/search"
PUBLICATION_NUMBER = "11460365"
ROWS_PER_PAGE = 100
DEFAULT_DELAY = 1.5

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": CONF_URL,
    "X-Requested-With": "XMLHttpRequest",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# REST API mode
# ---------------------------------------------------------------------------

def _api_params(page: int) -> dict:
    return {
        "newsearch": "true",
        "queryText": "",
        "pageNumber": page,
        "rowsPerPage": ROWS_PER_PAGE,
        "publication-number": PUBLICATION_NUMBER,
    }


def fetch_page_api(session: requests.Session, page: int) -> dict:
    resp = session.get(API_BASE, params=_api_params(page), timeout=30)
    if resp.status_code == 403:
        log.error(
            "HTTP 403 from IEEE Xplore REST API.\n"
            "  Fix A: pass your browser session cookie with --cookie 'JSESSIONID=...'\n"
            "         (copy from DevTools → Network → any /rest/search request → Cookie header)\n"
            "  Fix B: use --browser to drive a real Chromium instance instead."
        )
        sys.exit(1)
    resp.raise_for_status()
    return resp.json()


def scrape_api(delay: float, cookie: str | None) -> list[dict]:
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    if cookie:
        session.headers["Cookie"] = cookie

    log.info("REST API mode — fetching page 1 to get total record count...")
    first = fetch_page_api(session, page=1)
    total = first.get("totalRecords", 0)
    if not total:
        log.error("totalRecords = 0. The API response may have changed.")
        sys.exit(1)

    log.info(f"Total papers: {total}")
    records: list[dict] = first.get("records", [])
    total_pages = (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE

    for page in tqdm(range(2, total_pages + 1), desc="Fetching pages"):
        time.sleep(delay)
        data = fetch_page_api(session, page=page)
        records.extend(data.get("records", []))

    return records


# ---------------------------------------------------------------------------
# Playwright / browser mode
# ---------------------------------------------------------------------------

# JavaScript run inside the browser context via page.evaluate().
# fetch() inherits the browser's cookies and TLS fingerprint, so IEEE Xplore
# sees it as a first-party same-origin request — no IP blocking applies.
_JS_FETCH = """
async ([apiBase, pubNum, pageNum, rows]) => {
    const params = new URLSearchParams({
        newsearch:            'true',
        queryText:            '',
        pageNumber:           String(pageNum),
        rowsPerPage:          String(rows),
        'publication-number': pubNum,
    });
    const resp = await fetch(apiBase + '?' + params, {
        headers: {
            'Accept':           'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
        },
        credentials: 'include',
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
}
"""


def _browser_fetch(page, page_num: int) -> dict:
    return page.evaluate(_JS_FETCH, [API_BASE, PUBLICATION_NUMBER, page_num, ROWS_PER_PAGE])


def scrape_browser(delay: float) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "Playwright is not installed.\n"
            "  pip install playwright && playwright install chromium"
        )
        sys.exit(1)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=BASE_HEADERS["User-Agent"],
            locale="en-US",
        )
        bpage = context.new_page()

        # Load the proceedings page once — this establishes cookies/session.
        log.info("Browser mode — loading proceedings page to establish session...")
        bpage.goto(CONF_URL, wait_until="networkidle", timeout=60_000)

        # All API calls run as fetch() inside the browser's JS context.
        log.info("Fetching page 1 via in-browser fetch()...")
        first = _browser_fetch(bpage, 1)
        total = first.get("totalRecords", 0)
        if not total:
            log.error(
                "totalRecords = 0.\n"
                "  The page may not have loaded correctly, or the API path has changed.\n"
                "  Try running with headless=False to inspect the browser visually."
            )
            browser.close()
            sys.exit(1)

        log.info(f"Total papers: {total}")
        records: list[dict] = first.get("records", [])
        total_pages = (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE

        for page_num in tqdm(range(2, total_pages + 1), desc="Fetching pages"):
            time.sleep(delay)
            data = _browser_fetch(bpage, page_num)
            records.extend(data.get("records", []))

        browser.close()

    return records


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def structure(rec: dict) -> dict:
    article_number = rec.get("articleNumber", "")
    return {
        "title": rec.get("articleTitle", ""),
        "abstract": rec.get("abstract", ""),
        "authors": [a.get("preferredName", "") for a in rec.get("authors", [])],
        "doi": rec.get("doi", ""),
        "url": f"https://ieeexplore.ieee.org/document/{article_number}",
    }


def save(records: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    papers = [structure(r) for r in records]

    json_path = output_dir / "papers.json"
    json_path.write_text(json.dumps(papers, indent=2, ensure_ascii=False))
    log.info(f"JSON → {json_path}")
    log.info(f"Done. {len(papers)} papers saved.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape ICASSP 2026 paper metadata from IEEE Xplore.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Use Playwright Chromium instead of the REST API (needs: pip install playwright && playwright install chromium)",
    )
    parser.add_argument(
        "--cookie",
        metavar="STRING",
        help="Full Cookie header value copied from browser DevTools (REST API mode only)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        metavar="SECONDS",
        help=f"Pause between requests (default: {DEFAULT_DELAY}s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        metavar="DIR",
        help="Directory for output files (default: ./output)",
    )
    args = parser.parse_args()

    if args.browser:
        records = scrape_browser(delay=args.delay)
    else:
        records = scrape_api(delay=args.delay, cookie=args.cookie)

    if records:
        save(records, args.output)


if __name__ == "__main__":
    main()
