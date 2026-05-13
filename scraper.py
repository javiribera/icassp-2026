#!/usr/bin/env python3
"""
Scrapes paper metadata from the ICASSP 2026 proceedings on IEEE Xplore.
https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding

Calls the internal XHR endpoint used by the IEEE Xplore SPA.
If the endpoint returns 403, pass --cookie with a session cookie copied
from your browser (DevTools → Network → any /rest/search request → Cookie).

Detail enrichment
-----------------
The /rest/search endpoint returns truncated abstracts and no author affiliations.
After the initial scrape the script fetches /rest/document/{articleNumber}/ for
every paper to get the full abstract and per-author affiliation strings.
Uses a ThreadPoolExecutor (--workers, default 8).
Pass --no-details to skip this step.

Checkpoint / resume
-------------------
A checkpoint is saved to <output>/checkpoint.json after the search phase and
every 200 papers during detail fetching. If the script is interrupted, re-run
with the same --output directory to resume from where it left off.
Checkpointing is disabled when --limit is used.

Output: output/papers.json — array of objects:
  { title, abstract, authors: [{name, affiliation}], doi, url }
"""

import argparse
import concurrent.futures
import csv
import json
import logging
import sys
import time
from collections.abc import Callable
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
CHECKPOINT_FILE = "checkpoint.json"
CHECKPOINT_INTERVAL = 200  # save checkpoint every N completed detail fetches

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
# Checkpoint / resume
# ---------------------------------------------------------------------------

def load_checkpoint(
    output_dir: Path,
) -> tuple[list[dict], dict[str, dict]] | None:
    cp_path = output_dir / CHECKPOINT_FILE
    if not cp_path.exists():
        return None
    try:
        data = json.loads(cp_path.read_text())
        return data["records"], data["details_map"]
    except Exception as e:
        log.warning(f"Could not read checkpoint ({e}); starting fresh.")
        return None


def save_checkpoint(
    output_dir: Path,
    records: list[dict],
    details_map: dict[str, dict],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cp_path = output_dir / CHECKPOINT_FILE
    try:
        cp_path.write_text(
            json.dumps(
                {"records": records, "details_map": details_map},
                ensure_ascii=False,
            )
        )
    except OSError as e:
        log.warning(f"Could not save checkpoint: {e}")


# ---------------------------------------------------------------------------
# REST API mode — search
# ---------------------------------------------------------------------------

def _api_payload(page: int) -> dict:
    return {
        "newsearch": "true",
        "queryText": "",
        "pageNumber": page,
        "rowsPerPage": ROWS_PER_PAGE,
        "punumber": PUBLICATION_NUMBER,
    }


def fetch_page_api(session: requests.Session, page: int) -> dict:
    for attempt in range(3):
        resp = session.post(API_BASE, json=_api_payload(page), timeout=30)
        if resp.status_code == 403:
            log.error(
                "HTTP 403 from IEEE Xplore REST API.\n"
                "  Pass your browser session cookie with --cookie 'JSESSIONID=...'\n"
                "  (copy from DevTools → Network → any /rest/search request → Cookie header)"
            )
            sys.exit(1)
        if resp.status_code in (429, 503):
            wait = 10 * (2 ** attempt)
            log.warning(
                f"Rate limited (HTTP {resp.status_code}) on page {page}; "
                f"retrying in {wait}s..."
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    log.error(f"Page {page} still failing after 3 attempts; aborting.")
    sys.exit(1)


def scrape_api(
    session: requests.Session,
    delay: float,
    on_progress: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    log.info("REST API mode — fetching page 1 to get total record count...")
    first = fetch_page_api(session, page=1)
    total = first.get("totalRecords", 0)
    if not total:
        log.error("totalRecords = 0. The API response may have changed.")
        sys.exit(1)

    log.info(f"Total papers: {total}")
    records: list[dict] = first.get("records", [])
    total_pages = (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE

    if on_progress:
        try:
            on_progress(records)
        except Exception as e:
            log.warning(f"Could not write partial output: {e}; progress saves disabled.")
            on_progress = None

    for page in tqdm(range(2, total_pages + 1), desc="Fetching pages"):
        time.sleep(delay)
        data = fetch_page_api(session, page=page)
        records.extend(data.get("records", []))
        if on_progress:
            try:
                on_progress(records)
            except Exception as e:
                log.warning(f"Could not write partial output: {e}; progress saves disabled.")
                on_progress = None

    return records


# ---------------------------------------------------------------------------
# REST API mode — detail enrichment (concurrent)
# ---------------------------------------------------------------------------

def _fetch_doc_details(session: requests.Session, article_number: str) -> dict:
    """Return full abstract and per-author affiliations from the document endpoint.

    The /rest/search endpoint truncates abstracts. Full text is only available
    from /rest/document/{articleNumber}/ — confirmed by real-world scrapers.
    """
    for attempt in range(3):
        try:
            resp = session.get(f"{DOC_BASE}/{article_number}/", timeout=30)
            if resp.ok:
                data = resp.json()
                return {
                    "abstract": data.get("abstract", ""),
                    "affs": [
                        a.get("affiliation", "") for a in data.get("authors", [])
                    ],
                }
            if resp.status_code in (429, 503):
                time.sleep(5 * (2 ** attempt))
                continue
        except Exception:
            if attempt < 2:
                time.sleep(2 ** attempt)
    return {"abstract": "", "affs": []}


def fetch_details_api(
    records: list[dict],
    session: requests.Session,
    workers: int,
    delay: float = DEFAULT_DELAY,
    existing_details: dict[str, dict] | None = None,
    checkpoint_fn: Callable[[dict[str, dict]], None] | None = None,
) -> dict[str, dict]:
    """Fetch full abstract + affiliations from the per-paper document endpoint."""
    result: dict[str, dict] = dict(existing_details or {})
    pending = [r for r in records if r.get("articleNumber", "") not in result]

    if not pending:
        log.info("All paper details already fetched.")
        return result

    log.info(
        f"Fetching paper details via REST "
        f"({len(pending)} remaining of {len(records)}, {workers} workers)..."
    )
    completed = 0

    def _task(rec: dict) -> tuple[str, dict]:
        num = rec.get("articleNumber", "")
        det = _fetch_doc_details(session, num)
        time.sleep(delay)
        return num, det

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_task, rec) for rec in pending]
        for fut in tqdm(
            concurrent.futures.as_completed(futs), total=len(pending), desc="Details"
        ):
            num, details = fut.result()
            result[num] = details
            completed += 1
            if checkpoint_fn and completed % CHECKPOINT_INTERVAL == 0:
                checkpoint_fn(result)

    return result


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
        "venue": rec.get("publicationTitle", ""),
        "abstract": abstract,
        "authors": authors,
        "doi": rec.get("doi", ""),
        "url": f"https://ieeexplore.ieee.org/document/{article_number}",
    }


def save(
    records: list[dict],
    details_map: dict[str, dict],
    output_dir: Path,
    *,
    partial: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    papers = [structure(r, details_map) for r in records]
    json_path = output_dir / "papers.json"
    json_path.write_text(json.dumps(papers, indent=2, ensure_ascii=False))
    csv_path = output_dir / "papers.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "abstract", "url"])
        writer.writeheader()
        writer.writerows({"title": p["title"], "abstract": p["abstract"], "url": p["url"]} for p in papers)

    if partial:
        log.info(
            f"Partial output → {json_path} "
            f"({len(papers)} papers; abstracts truncated, affiliations pending)"
        )
        return
    log.info(f"JSON → {json_path}")
    log.info(f"CSV  → {csv_path}")
    no_abstract = sum(1 for p in papers if not p["abstract"])
    no_aff = sum(
        1 for p in papers if any(not a["affiliation"] for a in p["authors"])
    )
    log.info(
        f"Done. {len(papers)} papers saved "
        f"({no_abstract} missing abstract, {no_aff} missing ≥1 affiliation)."
    )


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
        "--cookie",
        metavar="STRING",
        help="Full Cookie header value from browser DevTools",
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
        default=None,
        metavar="N",
        help=f"Concurrent workers for detail fetching (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--no-details",
        "--no-affiliations",
        dest="no_details",
        action="store_true",
        help="Skip per-paper detail fetching (faster; affiliations will be empty and abstracts may be truncated)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Fetch only the first N papers (useful for testing; disables checkpointing)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        metavar="DIR",
        help="Output directory (default: ./output)",
    )
    args = parser.parse_args()

    workers = args.workers if args.workers is not None else DEFAULT_WORKERS
    fetch_details = not args.no_details
    use_checkpoint = args.limit is None
    records: list[dict] = []
    details_map: dict[str, dict] = {}

    if use_checkpoint:
        cp = load_checkpoint(args.output)
        if cp:
            records, details_map = cp
            log.info(
                f"Resuming from checkpoint: {len(records)} papers, "
                f"{len(details_map)} details already fetched."
            )

    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    if args.cookie:
        session.headers["Cookie"] = args.cookie

    if not records:
        progress_fn: Callable[[list[dict]], None] | None = (
            (lambda recs: save(recs, {}, args.output, partial=True))
            if fetch_details
            else None
        )
        records = scrape_api(session, delay=args.delay, on_progress=progress_fn)
        if args.limit is not None:
            records = records[: args.limit]
        if use_checkpoint:
            save_checkpoint(args.output, records, details_map)

    if fetch_details:
        details_map = fetch_details_api(
            records,
            session,
            workers=workers,
            delay=args.delay,
            existing_details=details_map if details_map else None,
            checkpoint_fn=(
                (lambda dm: save_checkpoint(args.output, records, dm))
                if use_checkpoint
                else None
            ),
        )

    if records:
        save(records, details_map, args.output)
        if use_checkpoint:
            cp_path = args.output / CHECKPOINT_FILE
            if cp_path.exists():
                cp_path.unlink()
                log.info("Checkpoint removed.")


if __name__ == "__main__":
    main()
