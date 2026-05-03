#!/usr/bin/env python3
"""
Scrapes paper metadata from the ICASSP 2026 proceedings page on IEEE Xplore.
Uses the internal REST API that the proceedings SPA calls, avoiding JS rendering.
Outputs papers.json (raw API response) and papers.csv (flattened).
"""

import argparse
import csv
import json
import logging
import time
from pathlib import Path

import requests
from tqdm import tqdm

CONF_URL = "https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding"
API_BASE = "https://ieeexplore.ieee.org/rest/search"
PUBLICATION_NUMBER = "11460365"
ROWS_PER_PAGE = 100
DEFAULT_DELAY = 1.5  # polite crawl delay in seconds

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": CONF_URL,
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def fetch_page(session: requests.Session, page: int) -> dict:
    params = {
        "newsearch": "true",
        "queryText": "",
        "pageNumber": page,
        "rowsPerPage": ROWS_PER_PAGE,
        "publication-number": PUBLICATION_NUMBER,
    }
    resp = session.get(API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def scrape(delay: float) -> list[dict]:
    session = requests.Session()
    session.headers.update(HEADERS)

    log.info("Fetching page 1 to determine total record count...")
    first = fetch_page(session, page=1)
    total = first.get("totalRecords", 0)
    if not total:
        log.error("Got 0 total records — the API may have changed or blocked the request.")
        return []

    log.info(f"Total papers: {total}")
    records: list[dict] = first.get("records", [])
    total_pages = (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE

    for page in tqdm(range(2, total_pages + 1), desc="Pages"):
        time.sleep(delay)
        data = fetch_page(session, page=page)
        records.extend(data.get("records", []))

    return records


def flatten(rec: dict) -> dict:
    authors = "; ".join(a.get("preferredName", "") for a in rec.get("authors", []))
    article_number = rec.get("articleNumber", "")
    return {
        "title": rec.get("articleTitle", ""),
        "authors": authors,
        "doi": rec.get("doi", ""),
        "article_number": article_number,
        "start_page": rec.get("startPage", ""),
        "end_page": rec.get("endPage", ""),
        "abstract": rec.get("abstract", ""),
        "keywords": "; ".join(
            kw.get("kwd", "")
            for group in rec.get("keywords", [])
            for kw in group.get("kwd", [])
            if isinstance(kw, dict)
        ),
        "url": f"https://ieeexplore.ieee.org/document/{article_number}",
    }


def save(records: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "papers.json"
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    log.info(f"Raw JSON  → {json_path}")

    flat = [flatten(r) for r in records]
    csv_path = output_dir / "papers.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
        writer.writeheader()
        writer.writerows(flat)
    log.info(f"Flat CSV  → {csv_path}")

    log.info(f"Done. {len(records)} papers saved.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape ICASSP 2026 paper metadata from IEEE Xplore."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        metavar="SECONDS",
        help=f"Pause between page requests (default: {DEFAULT_DELAY}s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        metavar="DIR",
        help="Directory for output files (default: ./output)",
    )
    args = parser.parse_args()

    records = scrape(delay=args.delay)
    if records:
        save(records, args.output)


if __name__ == "__main__":
    main()
