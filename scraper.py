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

Output: output/papers.json  (raw API records)
        output/papers.csv   (flat table)
"""

import argparse
import csv
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

def scrape_browser(delay: float) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "Playwright is not installed.\n"
            "  pip install playwright && playwright install chromium"
        )
        sys.exit(1)

    records: list[dict] = []
    total: int = 0
    page_num: int = 1

    def _handle_response(response):
        nonlocal total
        if "rest/search" in response.url and "publication-number" in response.url:
            try:
                data = response.json()
                if "totalRecords" in data:
                    total = data["totalRecords"]
                records.extend(data.get("records", []))
            except Exception:
                pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=BASE_HEADERS["User-Agent"],
            locale="en-US",
        )
        page = context.new_page()
        page.on("response", _handle_response)

        log.info("Browser mode — loading proceedings page (page 1)...")
        page.goto(CONF_URL, wait_until="networkidle", timeout=60_000)
        log.info(f"Total papers: {total}")

        total_pages = (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE

        for page_num in tqdm(range(2, total_pages + 1), desc="Navigating pages"):
            time.sleep(delay)
            page_url = f"{CONF_URL}?pageNumber={page_num}"
            page.goto(page_url, wait_until="networkidle", timeout=60_000)

        browser.close()

    return records


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def flatten(rec: dict) -> dict:
    authors = "; ".join(a.get("preferredName", "") for a in rec.get("authors", []))
    article_number = rec.get("articleNumber", "")
    keywords = "; ".join(
        kw if isinstance(kw, str) else kw.get("kwd", "")
        for group in rec.get("keywords", [])
        for kw in group.get("kwd", [])
    )
    return {
        "title": rec.get("articleTitle", ""),
        "authors": authors,
        "doi": rec.get("doi", ""),
        "article_number": article_number,
        "start_page": rec.get("startPage", ""),
        "end_page": rec.get("endPage", ""),
        "abstract": rec.get("abstract", ""),
        "keywords": keywords,
        "url": f"https://ieeexplore.ieee.org/document/{article_number}",
    }


def save(records: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "papers.json"
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    log.info(f"Raw JSON → {json_path}")

    flat = [flatten(r) for r in records]
    csv_path = output_dir / "papers.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
        writer.writeheader()
        writer.writerows(flat)
    log.info(f"Flat CSV → {csv_path}")
    log.info(f"Done. {len(records)} papers saved.")


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
