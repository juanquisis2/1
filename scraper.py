"""
Koha OPAC scraper for biblioteca.uwiener.edu.pe
Scrapes ~13,950 results and stores them in a SQLite database (biblioteca.db).

Usage:
    python3 scraper.py

Optional environment variables:
    PAGE_SIZE   - results per request (default: 20, max: 100)
    DELAY       - seconds between requests (default: 1.0)
    START_PAGE  - page number to resume from (default: 0, i.e. offset 0)
"""

import sqlite3
import time
import os
import sys
import logging
from datetime import datetime

import re
from datetime import timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = (
    "https://biblioteca.uwiener.edu.pe/cgi-bin/koha/opac-search.pl"
    "?advsearch=1&weight_search=1&limit=mc-itype%2Cphr%3A01"
    "&sort_by=relevance&do=Buscar"
)
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", 20))
DELAY = float(os.environ.get("DELAY", 1.0))
START_OFFSET = int(os.environ.get("START_PAGE", 0)) * PAGE_SIZE
DB_FILE = "biblioteca.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LibraryScraper/1.0; "
        "+https://github.com/juanquisis2/1)"
    ),
    "Accept-Language": "es,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS books (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            biblio_number  TEXT UNIQUE,
            title          TEXT,
            author         TEXT,
            publisher      TEXT,
            pub_year       TEXT,
            isbn           TEXT,
            item_type      TEXT,
            location       TEXT,
            call_number    TEXT,
            availability   TEXT,
            detail_url     TEXT,
            scraped_at     TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            offset     INTEGER,
            status     TEXT,
            items      INTEGER,
            logged_at  TEXT
        )
    """)
    conn.commit()


def insert_books(conn: sqlite3.Connection, books: list[dict]) -> int:
    inserted = 0
    for book in books:
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO books
                    (biblio_number, title, author, publisher, pub_year,
                     isbn, item_type, location, call_number,
                     availability, detail_url, scraped_at)
                VALUES
                    (:biblio_number, :title, :author, :publisher, :pub_year,
                     :isbn, :item_type, :location, :call_number,
                     :availability, :detail_url, :scraped_at)
                """,
                book,
            )
            inserted += conn.execute("SELECT changes()").fetchone()[0]
        except sqlite3.Error as exc:
            log.warning("DB insert error: %s | book: %s", exc, book.get("title"))
    conn.commit()
    return inserted


def log_offset(conn: sqlite3.Connection, offset: int, status: str, items: int) -> None:
    conn.execute(
        "INSERT INTO scrape_log (offset, status, items, logged_at) VALUES (?, ?, ?, ?)",
        (offset, status, items, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def already_scraped(conn: sqlite3.Connection, offset: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM scrape_log WHERE offset = ? AND status = 'ok'",
        (offset,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_page(html: str, offset: int) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []

    # Koha OPAC wraps each result in <li class="... biblio-result ...">
    results = soup.select("li.biblio-result")
    if not results:
        # Fallback: some Koha versions use a different selector
        results = soup.select("div.searchresults li")

    for result in results:
        book: dict = {
            "biblio_number": None,
            "title": None,
            "author": None,
            "publisher": None,
            "pub_year": None,
            "isbn": None,
            "item_type": None,
            "location": None,
            "call_number": None,
            "availability": None,
            "detail_url": None,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

        # Detail link & biblio number
        title_tag = result.select_one("a.title")
        if title_tag is None:
            title_tag = result.select_one("a[href*='biblionumber']")
        if title_tag:
            book["title"] = title_tag.get_text(" ", strip=True)
            href = title_tag.get("href", "")
            book["detail_url"] = (
                "https://biblioteca.uwiener.edu.pe" + href if href.startswith("/") else href
            )
            # Extract biblionumber from URL
            if "biblionumber=" in href:
                book["biblio_number"] = href.split("biblionumber=")[-1].split("&")[0]

        # Author
        author_tag = result.select_one("span.author")
        if author_tag is None:
            author_tag = result.select_one("a[href*='author']")
        if author_tag:
            book["author"] = author_tag.get_text(" ", strip=True)

        # Publisher / year — often inside <span class="results_summary publisher">
        pub_tag = result.select_one(".publisher")
        if pub_tag:
            pub_text = pub_tag.get_text(" ", strip=True)
            book["publisher"] = pub_text

        # Year — looks for 4-digit year
        full_text = result.get_text(" ", strip=True)
        year_match = re.search(r"\b(1[89]\d{2}|20\d{2})\b", full_text)
        if year_match:
            book["pub_year"] = year_match.group(1)

        # ISBN
        isbn_tag = result.select_one(".isbn")
        if isbn_tag:
            book["isbn"] = isbn_tag.get_text(" ", strip=True)
        else:
            isbn_match = re.search(r"\b97[89][\d\-]{10,}\b", full_text)
            if isbn_match:
                book["isbn"] = isbn_match.group(0)

        # Item type
        itype_tag = result.select_one(".itype")
        if itype_tag:
            book["item_type"] = itype_tag.get_text(" ", strip=True)

        # Availability
        avail_tag = result.select_one(".availabilitybox, .availability, .status")
        if avail_tag:
            book["availability"] = avail_tag.get_text(" ", strip=True)

        # Call number
        cn_tag = result.select_one(".call_number, .cn_sort, .shelf-location")
        if cn_tag:
            book["call_number"] = cn_tag.get_text(" ", strip=True)

        items.append(book)

    return items


def get_total_results(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    # Koha shows e.g. "Se encontraron 13950 resultados" or "Results 1-20 of 13950"
    for tag in soup.select(".results_summary, #numresults, .resultscount, strong"):
        m = re.search(r"[\d,\.]+", tag.get_text())
        if m:
            n = int(m.group(0).replace(",", "").replace(".", ""))
            if n > 100:
                return n
    # Search entire text
    m = re.search(r"(\d[\d,\.]{2,})\s*result", soup.get_text(), re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", "").replace(".", ""))
    return 0


# ---------------------------------------------------------------------------
# Main scrape loop
# ---------------------------------------------------------------------------

def fetch_page(session: requests.Session, offset: int) -> str | None:
    url = f"{BASE_URL}&offset={offset}&count={PAGE_SIZE}"
    for attempt in range(1, 4):
        try:
            response = session.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            log.warning("Attempt %d failed for offset %d: %s", attempt, offset, exc)
            time.sleep(attempt * 5)
    return None


def main() -> None:
    conn = sqlite3.connect(DB_FILE)
    init_db(conn)

    session = requests.Session()

    # Fetch first page to determine total
    log.info("Fetching first page to determine total results …")
    html = fetch_page(session, 0)
    if not html:
        log.error("Could not fetch the first page. Check connectivity and URL.")
        sys.exit(1)

    total = get_total_results(html)
    if total == 0:
        log.warning("Could not detect total results count; defaulting to 14000.")
        total = 14000
    log.info("Total results: %d", total)

    # Process first page (offset 0)
    offset = 0
    while offset < total:
        if offset > 0 and already_scraped(conn, offset):
            log.info("Offset %d already scraped, skipping.", offset)
            offset += PAGE_SIZE
            continue

        if offset == 0:
            page_html = html  # reuse already-fetched first page
        else:
            page_html = fetch_page(session, offset)

        if page_html is None:
            log.error("Skipping offset %d after repeated failures.", offset)
            log_offset(conn, offset, "error", 0)
            offset += PAGE_SIZE
            continue

        books = parse_page(page_html, offset)
        if not books and offset > 0:
            log.info("No results at offset %d — assuming end of results.", offset)
            break

        inserted = insert_books(conn, books)
        log_offset(conn, offset, "ok", len(books))
        total_in_db = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        log.info(
            "Offset %5d | page items: %2d | new: %2d | DB total: %d",
            offset, len(books), inserted, total_in_db,
        )

        offset += PAGE_SIZE
        if offset <= total:
            time.sleep(DELAY)

    total_in_db = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    log.info("Scrape complete. %d books stored in %s", total_in_db, DB_FILE)
    conn.close()


if __name__ == "__main__":
    main()
