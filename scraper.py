#!/usr/bin/env python3
"""
Scrapes paper metadata from the ICASSP 2026 proceedings on IEEE Xplore.
https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding

Strategy
--------
1. REST API mode (default):
   Calls the internal XHR endpoint used by the IEEE Xplore SPA.
   If the endpoint returns 403, pass --cookie with a session cookie copied
   from your browser (DevTools → Network → any /rest/search request → Cookie).

2. Browser mode (--browser, always works):
   Drives a real Chromium instance via Playwright. Loads the proceedings page
   once to establish a session, then calls fetch() from inside the browser's
   JS context for every paginated request and every document detail call.
   Requires:  pip install playwright && playwright install chromium

Affiliation enrichment
----------------------
Author affiliations are absent from the search results. After the initial
scrape the script fetches /rest/document/{articleNumber}/ for every paper.
REST mode uses a ThreadPoolExecutor (--workers, default 8). Browser mode
batches the calls via Promise.all() inside the browser's JS context.
Pass --no-affiliations to skip this step.

Output: output/papers.json — array of objects:
  { title, abstract, authors: [{name, affiliation}], doi, url }
"""

import argparse
import concurrent.futures
import json
import logging
import sys
import time
from pathlib import Path

import requests
from tqdm import tqdm

CONF_URL = "https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding"
API_BASE = "https://ieeexplore.ieee.org/rest/search"
DOC_BASE = "https://ieeexplore.ieee.org/rest/document"
PUBLICATION_NUMBER = "11460365"
ROWS_PER_PAGE = 100
DEFAULT_DELAY = 1.5
DEFAULT_WORKERS = 8
AFF_BATCH_SIZE = 50  # papers per Promise.all() batch in browser mode

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
# REST API mode — search
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


def scrape_api(session: requests.Session, delay: float) -> list[dict]:
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
# REST API mode — affiliation enrichment (concurrent)
# ---------------------------------------------------------------------------

def _fetch_doc_details(session: requests.Session, article_number: str) -> dict:
    """Return full abstract and per-author affiliations from the document endpoint.

    The /rest/search endpoint truncates abstracts. Full text is only available
    from /rest/document/{articleNumber}/ — confirmed by real-world scrapers.
    """
    try:
        resp = session.get(f"{DOC_BASE}/{article_number}/", timeout=30)
        if resp.ok:
            data = resp.json()
            return {
                "abstract": data.get("abstract", ""),
                "affs": [a.get("affiliation", "") for a in data.get("authors", [])],
            }
    except Exception:
        pass
    return {"abstract": "", "affs": []}


def fetch_details_api(
    records: list[dict],
    session: requests.Session,
    workers: int,
) -> dict[str, dict]:
    """Fetch full abstract + affiliations from the per-paper document endpoint."""
    log.info(f"Fetching paper details via REST ({len(records)} papers, {workers} workers)...")

    def _task(rec: dict) -> tuple[str, dict]:
        num = rec.get("articleNumber", "")
        return num, _fetch_doc_details(session, num)

    result: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_task, rec) for rec in records]
        for fut in tqdm(concurrent.futures.as_completed(futs), total=len(records), desc="Details"):
            num, details = fut.result()
            result[num] = details

    return result


# ---------------------------------------------------------------------------
# Playwright / browser mode — search
# ---------------------------------------------------------------------------

# Runs inside the browser's JS context; inherits cookies/session automatically.
_JS_FETCH_SEARCH = """
async ([apiBase, pubNum, pageNum, rows]) => {
    const params = new URLSearchParams({
        newsearch:            'true',
        queryText:            '',
        pageNumber:           String(pageNum),
        rowsPerPage:          String(rows),
        'publication-number': pubNum,
    });
    const resp = await fetch(apiBase + '?' + params, {
        headers: { 'Accept': 'application/json, text/plain, */*',
                   'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'include',
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
}
"""

# Fetches a batch of document detail endpoints in parallel via Promise.all().
# Returns [{abstract, affs}] for each article number in the batch.
# Abstract from /rest/search is truncated; the full text is here.
_JS_FETCH_DETAILS = """
async ([docBase, articleNumbers]) => {
    return Promise.all(articleNumbers.map(async (num) => {
        try {
            const resp = await fetch(docBase + '/' + num + '/', {
                headers: { 'Accept': 'application/json, text/plain, */*',
                           'X-Requested-With': 'XMLHttpRequest' },
                credentials: 'include',
            });
            if (!resp.ok) return {abstract: '', affs: []};
            const data = await resp.json();
            return {
                abstract: data.abstract || '',
                affs: (data.authors || []).map(a => a.affiliation || ''),
            };
        } catch { return {abstract: '', affs: []}; }
    }));
}
"""


def _browser_fetch_search(bpage, page_num: int) -> dict:
    return bpage.evaluate(_JS_FETCH_SEARCH, [API_BASE, PUBLICATION_NUMBER, page_num, ROWS_PER_PAGE])


def _browser_fetch_details(bpage, article_numbers: list[str]) -> list[dict]:
    return bpage.evaluate(_JS_FETCH_DETAILS, [DOC_BASE, article_numbers])


def scrape_browser(delay: float, fetch_affs: bool) -> tuple[list[dict], dict[str, dict]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright is not installed.\n  pip install playwright && playwright install chromium")
        sys.exit(1)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            # Required when running as root (Cloud Run) or in containers with
            # limited /dev/shm (default in most container runtimes).
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(user_agent=BASE_HEADERS["User-Agent"], locale="en-US")
        bpage = context.new_page()

        log.info("Browser mode — loading proceedings page to establish session...")
        bpage.goto(CONF_URL, wait_until="networkidle", timeout=60_000)

        log.info("Fetching page 1 via in-browser fetch()...")
        first = _browser_fetch_search(bpage, 1)
        total = first.get("totalRecords", 0)
        if not total:
            log.error(
                "totalRecords = 0.\n"
                "  The page may not have loaded correctly, or the API path has changed."
            )
            browser.close()
            sys.exit(1)

        log.info(f"Total papers: {total}")
        records: list[dict] = first.get("records", [])
        total_pages = (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE

        for page_num in tqdm(range(2, total_pages + 1), desc="Fetching pages"):
            time.sleep(delay)
            data = _browser_fetch_search(bpage, page_num)
            records.extend(data.get("records", []))

        details_map: dict[str, dict] = {}
        if fetch_affs:
            log.info(f"Fetching paper details via browser ({len(records)} papers, batch={AFF_BATCH_SIZE})...")
            nums = [r.get("articleNumber", "") for r in records]
            for i in tqdm(range(0, len(nums), AFF_BATCH_SIZE), desc="Details"):
                batch = nums[i : i + AFF_BATCH_SIZE]
                batch_details = _browser_fetch_details(bpage, batch)
                for num, det in zip(batch, batch_details):
                    details_map[num] = det
                time.sleep(delay)

        browser.close()

    return records, details_map


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def structure(rec: dict, details_map: dict[str, dict]) -> dict:
    article_number = rec.get("articleNumber", "")
    raw_authors = rec.get("authors", [])
    details = details_map.get(article_number, {})
    affs = details.get("affs", [])
    authors = [
        {
            "name": a.get("preferredName", ""),
            "affiliation": affs[i] if i < len(affs) else a.get("affiliation", ""),
        }
        for i, a in enumerate(raw_authors)
    ]
    # Abstract from /rest/search is truncated; prefer the full version from
    # the document detail endpoint when available.
    abstract = details.get("abstract") or rec.get("abstract", "")
    return {
        "title": rec.get("articleTitle", ""),
        "abstract": abstract,
        "authors": authors,
        "doi": rec.get("doi", ""),
        "url": f"https://ieeexplore.ieee.org/document/{article_number}",
    }


def save(records: list[dict], details_map: dict[str, dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    papers = [structure(r, details_map) for r in records]
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
        help="Use Playwright Chromium (needs: pip install playwright && playwright install chromium)",
    )
    parser.add_argument(
        "--cookie",
        metavar="STRING",
        help="Full Cookie header value from browser DevTools (REST API mode only)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        metavar="SECONDS",
        help=f"Pause between requests (default: {DEFAULT_DELAY}s)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=f"Concurrent workers for affiliation fetching, REST mode only (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--no-affiliations",
        action="store_true",
        help="Skip author affiliation fetching (faster; affiliation fields will be empty strings)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        metavar="DIR",
        help="Output directory (default: ./output)",
    )
    args = parser.parse_args()

    fetch_affs = not args.no_affiliations
    records: list[dict] = []
    details_map: dict[str, dict] = {}

    if args.browser:
        records, details_map = scrape_browser(delay=args.delay, fetch_affs=fetch_affs)
    else:
        session = requests.Session()
        session.headers.update(BASE_HEADERS)
        if args.cookie:
            session.headers["Cookie"] = args.cookie
        records = scrape_api(session, delay=args.delay)
        if fetch_affs:
            details_map = fetch_details_api(records, session, workers=args.workers)

    if records:
        save(records, details_map, args.output)


if __name__ == "__main__":
    main()
