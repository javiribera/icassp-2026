#!/usr/bin/env python3
"""
Scrapes paper metadata from academic conference proceedings.

Supported conferences
  IEEE: icassp  — uses the internal IEEE Xplore JSON REST API
  CVF:  cvpr, iccv, wacv — parses openaccess.thecvf.com static HTML

Both backends produce the same output schema so estimate_relevance.py works
with any conference.

Usage:
  python scraper.py --conference cvpr  --year 2024
  python scraper.py --conference icassp --year 2026
  python scraper.py --conference cvpr  --year 2024 --limit 20   # smoke-test

IEEE note: if the endpoint returns 403, pass --cookie with a session cookie
copied from your browser (DevTools → Network → any /rest/search request →
Cookie header).

CVF note: CVPR2026 will return 403/empty until papers are published (~Jun 2026).
Test against a past year: --conference cvpr --year 2024.

Checkpoint / resume
  A checkpoint is saved after the listing phase and every 200 detail fetches.
  Re-run with the same --output directory to resume. Disabled when --limit is used.

Output: output/papers.json — array of objects:
  { title, venue, year, abstract, authors:[{name,affiliation}], doi, url, pdf_url }
Output: output/papers.csv — title, abstract, url (columns only)
"""

import argparse
import concurrent.futures
import csv
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ---------------------------------------------------------------------------
# IEEE constants
# ---------------------------------------------------------------------------

API_BASE = "https://ieeexplore.ieee.org/rest/search"
DOC_BASE = "https://ieeexplore.ieee.org/rest/document"
ROWS_PER_PAGE = 100

# (conference_lower, year) -> publication_number
IEEE_PUBNUMS: dict[tuple[str, int], str] = {
    ("icassp", 2026): "11460365",
}

BASE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "X-Requested-With": "XMLHttpRequest",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

# ---------------------------------------------------------------------------
# CVF constants
# ---------------------------------------------------------------------------

CVF_BASE = "https://openaccess.thecvf.com"
CVF_CONFERENCES = {"cvpr", "iccv", "wacv"}

CVF_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

DEFAULT_DELAY = 1.5
DEFAULT_WORKERS = 8
CHECKPOINT_FILE = "checkpoint.json"
CHECKPOINT_INTERVAL = 200

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Checkpoint / resume
# ---------------------------------------------------------------------------

def load_checkpoint(output_dir: Path) -> tuple[list[dict], set[str]] | None:
    cp_path = output_dir / CHECKPOINT_FILE
    if not cp_path.exists():
        return None
    try:
        data = json.loads(cp_path.read_text())
        return data["papers"], set(data["done"])
    except Exception as e:
        log.warning(f"Could not read checkpoint ({e}); starting fresh.")
        return None


def save_checkpoint(output_dir: Path, papers: list[dict], done: set[str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cp_path = output_dir / CHECKPOINT_FILE
    try:
        cp_path.write_text(
            json.dumps({"papers": papers, "done": list(done)}, ensure_ascii=False)
        )
    except OSError as e:
        log.warning(f"Could not save checkpoint: {e}")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _strip_internal(paper: dict) -> dict:
    return {k: v for k, v in paper.items() if not k.startswith("_")}


def save(papers: list[dict], output_dir: Path, *, partial: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    clean = [_strip_internal(p) for p in papers]
    json_path = output_dir / "papers.json"
    json_path.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
    csv_path = output_dir / "papers.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "abstract", "url"])
        writer.writeheader()
        writer.writerows(
            {"title": p["title"], "abstract": p["abstract"], "url": p["url"]}
            for p in clean
        )
    if partial:
        log.info(f"Partial output → {json_path} ({len(clean)} papers; details pending)")
        return
    log.info(f"JSON → {json_path}")
    log.info(f"CSV  → {csv_path}")
    no_abstract = sum(1 for p in clean if not p["abstract"])
    log.info(f"Done. {len(clean)} papers saved ({no_abstract} missing abstract).")


# ---------------------------------------------------------------------------
# Generic detail-enrichment loop (shared by both backends)
# ---------------------------------------------------------------------------

def enrich_details(
    papers: list[dict],
    session: requests.Session,
    fetch_one: Callable[[requests.Session, dict], None],
    workers: int,
    delay: float,
    done: set[str],
    checkpoint_fn: Callable[[set[str]], None] | None = None,
) -> set[str]:
    pending = [p for p in papers if p["_id"] not in done]
    if not pending:
        log.info("All paper details already fetched.")
        return done

    log.info(
        f"Fetching details ({len(pending)} remaining of {len(papers)}, {workers} workers)..."
    )
    completed = 0

    def _task(paper: dict) -> str:
        fetch_one(session, paper)
        time.sleep(delay)
        return paper["_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_task, p) for p in pending]
        for fut in tqdm(
            concurrent.futures.as_completed(futs), total=len(pending), desc="Details"
        ):
            done.add(fut.result())
            completed += 1
            if checkpoint_fn and completed % CHECKPOINT_INTERVAL == 0:
                checkpoint_fn(done)

    return done


# ---------------------------------------------------------------------------
# IEEE backend
# ---------------------------------------------------------------------------

def _ieee_fetch_page(
    session: requests.Session, page: int, pubnum: str
) -> dict:
    payload = {
        "newsearch": "true",
        "queryText": "",
        "pageNumber": page,
        "rowsPerPage": ROWS_PER_PAGE,
        "punumber": pubnum,
    }
    for attempt in range(3):
        resp = session.post(API_BASE, json=payload, timeout=30)
        if resp.status_code == 403:
            log.error(
                "HTTP 403 from IEEE Xplore REST API.\n"
                "  Pass your browser session cookie with --cookie 'JSESSIONID=...'\n"
                "  (copy from DevTools → Network → any /rest/search request → Cookie header)"
            )
            sys.exit(1)
        if resp.status_code in (429, 503):
            wait = 10 * (2 ** attempt)
            log.warning(f"Rate limited (HTTP {resp.status_code}); retrying in {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    log.error(f"Page {page} still failing after 3 attempts; aborting.")
    sys.exit(1)


def _ieee_paper(rec: dict, year: int) -> dict:
    article_number = rec.get("articleNumber", "")
    return {
        "title": rec.get("articleTitle", ""),
        "venue": rec.get("publicationTitle", ""),
        "year": year,
        "abstract": rec.get("abstract", ""),
        "authors": [
            {
                "name": a.get("preferredName", ""),
                "affiliation": a.get("affiliation", ""),
            }
            for a in rec.get("authors", [])
        ],
        "doi": rec.get("doi", ""),
        "url": f"https://ieeexplore.ieee.org/document/{article_number}",
        "pdf_url": "",
        "_id": article_number,
    }


def ieee_list(
    session: requests.Session,
    pubnum: str,
    year: int,
    delay: float,
    on_progress: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    log.info("IEEE — fetching page 1 to get total record count...")
    first = _ieee_fetch_page(session, page=1, pubnum=pubnum)
    total = first.get("totalRecords", 0)
    if not total:
        log.error("totalRecords = 0. The API response may have changed.")
        sys.exit(1)
    log.info(f"Total papers: {total}")
    papers = [_ieee_paper(r, year) for r in first.get("records", [])]
    total_pages = (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE

    if on_progress:
        try:
            on_progress(papers)
        except Exception as e:
            log.warning(f"Could not write partial output: {e}; progress saves disabled.")
            on_progress = None

    for page in tqdm(range(2, total_pages + 1), desc="Fetching pages"):
        time.sleep(delay)
        data = _ieee_fetch_page(session, page=page, pubnum=pubnum)
        papers.extend(_ieee_paper(r, year) for r in data.get("records", []))
        if on_progress:
            try:
                on_progress(papers)
            except Exception as e:
                log.warning(f"Could not write partial output: {e}; progress saves disabled.")
                on_progress = None

    return papers


def ieee_fetch_one(session: requests.Session, paper: dict) -> None:
    article_number = paper["_id"]
    for attempt in range(3):
        try:
            resp = session.get(f"{DOC_BASE}/{article_number}/", timeout=30)
            if resp.ok:
                data = resp.json()
                if data.get("abstract"):
                    paper["abstract"] = data["abstract"]
                affs = [a.get("affiliation", "") for a in data.get("authors", [])]
                for i, author in enumerate(paper["authors"]):
                    if i < len(affs) and affs[i]:
                        author["affiliation"] = affs[i]
                return
            if resp.status_code in (429, 503):
                time.sleep(5 * (2 ** attempt))
                continue
            log.debug(
                f"Unexpected HTTP {resp.status_code} for article {article_number} "
                f"(attempt {attempt + 1}/3)"
            )
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                log.debug(f"Could not fetch details for article {article_number}: {exc}")
    log.debug(f"Giving up on article {article_number} after 3 attempts; detail fields left as-is.")


# ---------------------------------------------------------------------------
# CVF backend
# ---------------------------------------------------------------------------

def _cvf_fetch_html(session: requests.Session, url: str) -> str | None:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 404:
                return None
            if resp.status_code == 403:
                log.warning(f"HTTP 403 for {url} (not published yet or access denied).")
                return None
            if resp.status_code in (429, 503):
                time.sleep(5 * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                log.warning(f"Could not fetch {url}: {exc}")
    return None


def _cvf_parse_listing(html: str, conf: str, year: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    papers = []
    for dt in soup.select("dt.ptitle"):
        a = dt.find("a")
        if not a:
            continue
        title = a.get_text(strip=True)
        href = str(a.get("href", ""))
        if not href:
            continue
        url = urljoin(CVF_BASE, href)
        papers.append(
            {
                "title": title,
                "venue": f"{conf.upper()} {year}",
                "year": year,
                "abstract": "",
                "authors": [],
                "doi": "",
                "url": url,
                "pdf_url": "",
                "_id": url,
            }
        )
    return papers


def cvf_list(
    session: requests.Session, conf: str, year: int, delay: float
) -> list[dict]:
    conf_tag = f"{conf.upper()}{year}"
    all_url = f"{CVF_BASE}/{conf_tag}?day=all"
    log.info(f"CVF — fetching {all_url} ...")
    html = _cvf_fetch_html(session, all_url)
    if html:
        papers = _cvf_parse_listing(html, conf, year)
        if papers:
            log.info(f"Found {len(papers)} papers via ?day=all")
            return papers

    # Fallback: discover per-day pages
    log.info(f"Falling back to per-day pages from {CVF_BASE}/{conf_tag} ...")
    root_html = _cvf_fetch_html(session, f"{CVF_BASE}/{conf_tag}")
    if not root_html:
        log.error(
            f"Could not fetch conference page for {conf.upper()} {year}.\n"
            "  The conference may not yet be published on openaccess.thecvf.com.\n"
            f"  Test with a past year: --conference {conf} --year 2024"
        )
        sys.exit(1)

    root_soup = BeautifulSoup(root_html, "html.parser")
    day_links = list(
        dict.fromkeys(
            urljoin(CVF_BASE, str(a["href"]))
            for a in root_soup.find_all("a", href=True)
            if "?day=" in str(a["href"]) and "day=all" not in str(a["href"])
        )
    )

    all_papers: dict[str, dict] = {}
    for day_url in tqdm(day_links, desc="Day pages"):
        time.sleep(delay)
        day_html = _cvf_fetch_html(session, day_url)
        if day_html:
            for p in _cvf_parse_listing(day_html, conf, year):
                all_papers.setdefault(p["_id"], p)

    if not all_papers:
        log.error(
            f"No papers found for {conf.upper()} {year}.\n"
            "  The conference may not yet be published on openaccess.thecvf.com."
        )
        sys.exit(1)

    log.info(f"Found {len(all_papers)} papers across {len(day_links)} day pages")
    return list(all_papers.values())


def cvf_fetch_one(session: requests.Session, paper: dict) -> None:
    html = _cvf_fetch_html(session, paper["url"])
    if not html:
        return
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.find(id="papertitle")
    if title_el:
        t = title_el.get_text(strip=True)
        if t:
            paper["title"] = t

    abstract_el = soup.find(id="abstract")
    if abstract_el:
        paper["abstract"] = abstract_el.get_text(strip=True)

    authors_el = soup.find(id="authors")
    if authors_el:
        # Authors are in <b><i>Name1, Name2, ...</i></b>
        bi = authors_el.find("b")
        raw = (bi or authors_el).get_text(strip=True)
        names = [n.strip() for n in re.split(r",\s*", raw) if n.strip()]
        paper["authors"] = [{"name": n, "affiliation": ""} for n in names]

    for a_tag in soup.find_all("a", href=True):
        href = str(a_tag.get("href", ""))
        if href.endswith("_paper.pdf"):
            paper["pdf_url"] = urljoin(CVF_BASE, href)
            break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape paper metadata from academic conference proceedings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--conference",
        required=True,
        metavar="CONF",
        help="Conference name: icassp | cvpr | iccv | wacv",
    )
    parser.add_argument(
        "--year",
        required=True,
        type=int,
        metavar="YEAR",
        help="Conference year (e.g. 2026)",
    )
    parser.add_argument(
        "--cookie",
        metavar="STRING",
        help="Full Cookie header value from browser DevTools (IEEE/ICASSP only)",
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
        help="Skip per-paper detail fetching (abstracts/affiliations may be empty)",
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

    conf = args.conference.lower()
    year = args.year
    workers = args.workers if args.workers is not None else DEFAULT_WORKERS
    fetch_details_flag = not args.no_details
    use_checkpoint = args.limit is None

    # Resolve backend
    pubnum: str = ""
    is_cvf = conf in CVF_CONFERENCES
    is_ieee = (conf, year) in IEEE_PUBNUMS

    if is_cvf:
        session = requests.Session()
        session.headers.update(CVF_HEADERS)
        fetch_one: Callable[[requests.Session, dict], None] = cvf_fetch_one
    elif is_ieee:
        pubnum = IEEE_PUBNUMS[(conf, year)]
        session = requests.Session()
        session.headers.update(BASE_HEADERS)
        session.headers["Referer"] = (
            f"https://ieeexplore.ieee.org/xpl/conhome/{pubnum}/proceeding"
        )
        if args.cookie:
            session.headers["Cookie"] = args.cookie
        fetch_one = ieee_fetch_one
    else:
        supported_ieee = ", ".join(
            f"{c} {y}" for (c, y) in sorted(IEEE_PUBNUMS)
        )
        log.error(
            f"Unknown conference/year: {conf!r} {year}.\n"
            f"  Supported CVF (any year): {', '.join(sorted(CVF_CONFERENCES))}\n"
            f"  Supported IEEE: {supported_ieee}"
        )
        sys.exit(1)

    papers: list[dict] = []
    done: set[str] = set()

    if use_checkpoint:
        cp = load_checkpoint(args.output)
        if cp:
            papers, done = cp
            log.info(
                f"Resuming from checkpoint: {len(papers)} papers, "
                f"{len(done)} details already fetched."
            )

    if not papers:
        if is_cvf:
            papers = cvf_list(session, conf, year, args.delay)
        else:
            progress_fn: Callable[[list[dict]], None] | None = (
                (lambda ps: save(ps, args.output, partial=True))
                if fetch_details_flag
                else None
            )
            papers = ieee_list(
                session, pubnum, year, args.delay, on_progress=progress_fn
            )

        if args.limit is not None:
            papers = papers[: args.limit]
        if use_checkpoint:
            save_checkpoint(args.output, papers, done)

    if fetch_details_flag:
        done = enrich_details(
            papers,
            session,
            fetch_one,
            workers=workers,
            delay=args.delay,
            done=done,
            checkpoint_fn=(
                (lambda d: save_checkpoint(args.output, papers, d))
                if use_checkpoint
                else None
            ),
        )

    if papers:
        save(papers, args.output)
        if use_checkpoint:
            cp_path = args.output / CHECKPOINT_FILE
            if cp_path.exists():
                cp_path.unlink()
                log.info("Checkpoint removed.")


if __name__ == "__main__":
    main()
