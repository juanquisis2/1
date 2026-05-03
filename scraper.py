#!/usr/bin/env python3
"""
Scraper for Biblioteca Universidad Wiener - Koha OPAC
Scrapes all bibliographic records from:
https://biblioteca.uwiener.edu.pe/cgi-bin/koha/opac-search.pl?advsearch=1&weight_search=1&limit=mc-itype%2Cphr%3A01&sort_by=relevance&do=Buscar

Results are stored in a SQLite database: biblioteca_wiener.db
"""

import requests
from bs4 import BeautifulSoup
import sqlite3
import time
import logging
import sys
import re
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = (
    "https://biblioteca.uwiener.edu.pe/cgi-bin/koha/opac-search.pl"
    "?advsearch=1&weight_search=1&limit=mc-itype%2Cphr%3A01"
    "&sort_by=relevance&do=Buscar"
)
RESULTS_PER_PAGE = 20          # Koha default page size
MAX_RESULTS = 20000            # Safety cap (actual count ~13950)
DB_FILE = "biblioteca_wiener.db"
DELAY_BETWEEN_REQUESTS = 1.0  # seconds – be polite to the server
REQUEST_TIMEOUT = 30           # seconds
MAX_RETRIES = 5
RETRY_BACKOFF = 5              # seconds between retries

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BibliotecaWienerScraper/1.0; "
        "research purposes)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es,en;q=0.5",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def init_db(db_file: str) -> sqlite3.Connection:
    """Create the database and tables if they don't exist."""
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS books (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            biblio_id   TEXT,
            title       TEXT,
            author      TEXT,
            publisher   TEXT,
            year        TEXT,
            isbn        TEXT,
            edition     TEXT,
            item_type   TEXT,
            location    TEXT,
            call_number TEXT,
            url         TEXT,
            scraped_at  DATETIME DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_books_biblio_id
            ON books (biblio_id);

        CREATE INDEX IF NOT EXISTS idx_books_title  ON books (title);
        CREATE INDEX IF NOT EXISTS idx_books_author ON books (author);

        CREATE TABLE IF NOT EXISTS scrape_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            page       INTEGER,
            offset     INTEGER,
            status     TEXT,
            records    INTEGER,
            timestamp  DATETIME DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    return conn


def upsert_book(conn: sqlite3.Connection, record: dict) -> None:
    """Insert or replace a book record."""
    conn.execute(
        """
        INSERT INTO books
            (biblio_id, title, author, publisher, year, isbn, edition,
             item_type, location, call_number, url)
        VALUES
            (:biblio_id, :title, :author, :publisher, :year, :isbn,
             :edition, :item_type, :location, :call_number, :url)
        ON CONFLICT(biblio_id) DO UPDATE SET
            title       = excluded.title,
            author      = excluded.author,
            publisher   = excluded.publisher,
            year        = excluded.year,
            isbn        = excluded.isbn,
            edition     = excluded.edition,
            item_type   = excluded.item_type,
            location    = excluded.location,
            call_number = excluded.call_number,
            url         = excluded.url,
            scraped_at  = datetime('now')
        """,
        record,
    )


def log_page(conn: sqlite3.Connection, page: int, offset: int,
             status: str, records: int) -> None:
    conn.execute(
        "INSERT INTO scrape_log (page, offset, status, records) "
        "VALUES (?, ?, ?, ?)",
        (page, offset, status, records),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch_page(session: requests.Session, url: str) -> requests.Response | None:
    """Fetch a URL with retries and exponential back-off."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            wait = RETRY_BACKOFF * attempt
            log.warning(
                "Attempt %d/%d failed for %s: %s – retrying in %ds",
                attempt, MAX_RETRIES, url, exc, wait,
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait)
    log.error("All %d attempts failed for %s", MAX_RETRIES, url)
    return None


def build_page_url(offset: int) -> str:
    """Append the offset parameter to the base search URL."""
    if offset == 0:
        return BASE_URL
    # Koha uses &offset=N to paginate
    return f"{BASE_URL}&offset={offset}"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_results_page(html: str, base_url: str) -> tuple[list[dict], int]:
    """
    Parse a Koha OPAC search results page.

    Returns:
        (records, total_results)
        - records: list of dicts with book metadata
        - total_results: total hit count reported by Koha (0 if not found)
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    # ---- Total results count ------------------------------------------------
    total_results = 0
    # Koha renders something like "Results 1-20 of 13950"
    for candidate in soup.find_all(
        string=re.compile(r"\d[\d,]*\s+result", re.IGNORECASE)
    ):
        m = re.search(r"([\d,]+)\s+result", candidate, re.IGNORECASE)
        if m:
            total_results = int(m.group(1).replace(",", ""))
            break
    if total_results == 0:
        # Alternative: search in result count span/div
        count_el = soup.find(id="numresults") or soup.find(class_="numresults")
        if count_el:
            m = re.search(r"([\d,]+)", count_el.get_text())
            if m:
                total_results = int(m.group(1).replace(",", ""))

    # ---- Result items -------------------------------------------------------
    # Koha OPAC wraps each result in <li class="search-result"> or similar
    result_items = soup.select("li.search-result, div.search-result")
    if not result_items:
        # Fallback: look for <div class="results"> rows
        result_items = soup.select("table#searchresults tr.even, "
                                   "table#searchresults tr.odd")

    for item in result_items:
        record = _parse_result_item(item, base_url)
        if record:
            records.append(record)

    return records, total_results


def _clean(text: str | None) -> str:
    """Strip and collapse whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _parse_result_item(item, base_url: str) -> dict | None:
    """Extract metadata from a single search-result element."""
    record: dict = {
        "biblio_id": "",
        "title": "",
        "author": "",
        "publisher": "",
        "year": "",
        "isbn": "",
        "edition": "",
        "item_type": "",
        "location": "",
        "call_number": "",
        "url": "",
    }

    # --- Biblio ID from a link like /cgi-bin/koha/opac-detail.pl?biblionumber=12345
    link_el = item.find("a", href=re.compile(r"biblionumber=(\d+)"))
    if link_el:
        m = re.search(r"biblionumber=(\d+)", link_el["href"])
        if m:
            record["biblio_id"] = m.group(1)
            record["url"] = urljoin(base_url, link_el["href"])

    if not record["biblio_id"]:
        return None  # Skip items without a biblio ID

    # --- Title
    title_el = (
        item.find(class_=re.compile(r"title", re.I))
        or item.find("a", href=re.compile(r"biblionumber="))
    )
    if title_el:
        record["title"] = _clean(title_el.get_text())

    # --- Author
    author_el = item.find(class_=re.compile(r"author", re.I))
    if author_el:
        record["author"] = _clean(author_el.get_text())

    # --- Publication info (publisher, year, edition)
    pub_el = item.find(class_=re.compile(r"publisher|pubdate|imprint", re.I))
    if pub_el:
        pub_text = _clean(pub_el.get_text())
        # Try to extract year
        m_year = re.search(r"\b(1[89]\d{2}|20[012]\d)\b", pub_text)
        if m_year:
            record["year"] = m_year.group(1)
        record["publisher"] = pub_text

    # If publisher not found via class, check all text for common patterns
    if not record["publisher"]:
        full_text = _clean(item.get_text(" "))
        m_year = re.search(r"\b(1[89]\d{2}|20[012]\d)\b", full_text)
        if m_year:
            record["year"] = m_year.group(1)

    # --- ISBN
    isbn_el = item.find(class_=re.compile(r"isbn", re.I))
    if isbn_el:
        record["isbn"] = _clean(isbn_el.get_text())
    else:
        full_text = item.get_text()
        m_isbn = re.search(r"ISBN[:\s]*([\d\-X]{10,17})", full_text, re.I)
        if m_isbn:
            record["isbn"] = m_isbn.group(1).strip()

    # --- Call number / location
    call_el = item.find(class_=re.compile(r"call.?number|ccode|location", re.I))
    if call_el:
        record["call_number"] = _clean(call_el.get_text())

    # --- Item type
    type_el = item.find(class_=re.compile(r"item.?type|itype", re.I))
    if type_el:
        record["item_type"] = _clean(type_el.get_text())

    return record


# ---------------------------------------------------------------------------
# Main scraping loop
# ---------------------------------------------------------------------------


def scrape_all(conn: sqlite3.Connection) -> int:
    """
    Iterate through all pages of search results and store records in the DB.
    Returns the total number of records inserted/updated.
    """
    session = requests.Session()
    total_scraped = 0
    total_results = None
    page_num = 1
    offset = 0

    while True:
        url = build_page_url(offset)
        log.info("Fetching page %d (offset=%d) …", page_num, offset)

        resp = fetch_page(session, url)
        if resp is None:
            log_page(conn, page_num, offset, "error", 0)
            log.error("Skipping page %d due to repeated failures.", page_num)
            offset += RESULTS_PER_PAGE
            page_num += 1
            if total_results and offset >= total_results:
                break
            if offset >= MAX_RESULTS:
                break
            continue

        records, discovered_total = parse_results_page(
            resp.text, resp.url
        )

        # On first page, discover the real total
        if total_results is None and discovered_total > 0:
            total_results = discovered_total
            log.info("Total results reported by server: %d", total_results)

        if not records:
            log.warning(
                "No records parsed on page %d (offset=%d). "
                "Ending scrape.",
                page_num, offset,
            )
            log_page(conn, page_num, offset, "empty", 0)
            break

        for rec in records:
            upsert_book(conn, rec)
        conn.commit()

        total_scraped += len(records)
        log_page(conn, page_num, offset, "ok", len(records))
        log.info(
            "  → %d records parsed (total so far: %d / %s)",
            len(records),
            total_scraped,
            total_results if total_results else "?",
        )

        offset += RESULTS_PER_PAGE
        page_num += 1

        # Stop conditions
        if total_results and offset >= total_results:
            log.info("Reached end of results (%d).", total_results)
            break
        if offset >= MAX_RESULTS:
            log.warning("Hit safety cap of %d records.", MAX_RESULTS)
            break

        time.sleep(DELAY_BETWEEN_REQUESTS)

    return total_scraped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=== Biblioteca Wiener OPAC Scraper ===")
    log.info("Target URL: %s", BASE_URL)
    log.info("Database  : %s", DB_FILE)

    conn = init_db(DB_FILE)
    try:
        total = scrape_all(conn)
        log.info("Scraping complete. Total records stored: %d", total)

        # Quick summary
        row = conn.execute("SELECT COUNT(*) FROM books").fetchone()
        log.info("Records in database: %d", row[0])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
