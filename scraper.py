#!/usr/bin/env python3
"""
Scraper for Biblioteca Universidad Wiener - Koha OPAC
======================================================
Scrapes all bibliographic records from:
  https://biblioteca.uwiener.edu.pe/cgi-bin/koha/opac-search.pl
      ?advsearch=1&weight_search=1&limit=mc-itype%2Cphr%3A01
      &sort_by=relevance&do=Buscar

Results are stored in a SQLite database (default: biblioteca_wiener.db).

Usage examples
--------------
  # Full scrape
  python scraper.py

  # Resume an interrupted scrape
  python scraper.py --resume

  # Use a custom database path and slower delay
  python scraper.py --db /data/library.db --delay 2.0

  # Only fetch first 100 records (useful for testing)
  python scraper.py --count 100

  # Export collected data to CSV (no network needed)
  python scraper.py --csv books.csv

  # Fetch richer metadata from each book's detail page (slow)
  python scraper.py --details
"""

import argparse
import csv
import logging
import re
import sqlite3
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Defaults (all overridable via CLI)
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = (
    "https://biblioteca.uwiener.edu.pe/cgi-bin/koha/opac-search.pl"
    "?advsearch=1&weight_search=1&limit=mc-itype%2Cphr%3A01"
    "&sort_by=relevance&do=Buscar"
)
DEFAULT_DETAIL_BASE = "https://biblioteca.uwiener.edu.pe"
RESULTS_PER_PAGE   = 20
MAX_RESULTS        = 20_000     # safety cap
DEFAULT_DB         = "biblioteca_wiener.db"
DEFAULT_DELAY      = 1.0        # seconds between list-page requests
DEFAULT_DETAIL_DELAY = 0.5      # seconds between detail-page requests
REQUEST_TIMEOUT    = 30
MAX_RETRIES        = 5
RETRY_BACKOFF      = 5          # base seconds between retry attempts

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
    """Create (or open) the SQLite database and ensure tables exist."""
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS books (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            biblio_id    TEXT,
            title        TEXT,
            author       TEXT,
            publisher    TEXT,
            year         TEXT,
            isbn         TEXT,
            edition      TEXT,
            item_type    TEXT,
            location     TEXT,
            call_number  TEXT,
            subjects     TEXT,
            description  TEXT,
            url          TEXT,
            detail_scraped INTEGER DEFAULT 0,
            scraped_at   DATETIME DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_books_biblio_id
            ON books (biblio_id);
        CREATE INDEX IF NOT EXISTS idx_books_title  ON books (title);
        CREATE INDEX IF NOT EXISTS idx_books_author ON books (author);

        CREATE TABLE IF NOT EXISTS scrape_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            page      INTEGER,
            offset    INTEGER,
            status    TEXT,
            records   INTEGER,
            timestamp DATETIME DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    return conn


def upsert_book(conn: sqlite3.Connection, record: dict) -> None:
    """Insert or update a book record (keyed on biblio_id)."""
    conn.execute(
        """
        INSERT INTO books
            (biblio_id, title, author, publisher, year, isbn, edition,
             item_type, location, call_number, subjects, description, url)
        VALUES
            (:biblio_id, :title, :author, :publisher, :year, :isbn,
             :edition, :item_type, :location, :call_number,
             :subjects, :description, :url)
        ON CONFLICT(biblio_id) DO UPDATE SET
            title        = excluded.title,
            author       = excluded.author,
            publisher    = excluded.publisher,
            year         = excluded.year,
            isbn         = excluded.isbn,
            edition      = excluded.edition,
            item_type    = excluded.item_type,
            location     = excluded.location,
            call_number  = excluded.call_number,
            subjects     = excluded.subjects,
            description  = excluded.description,
            url          = excluded.url,
            scraped_at   = datetime('now')
        """,
        record,
    )


def update_book_details(conn: sqlite3.Connection, biblio_id: str,
                        details: dict) -> None:
    """Patch detail fields onto an existing book row."""
    conn.execute(
        """
        UPDATE books SET
            publisher      = COALESCE(NULLIF(:publisher,''),  publisher),
            year           = COALESCE(NULLIF(:year,''),       year),
            isbn           = COALESCE(NULLIF(:isbn,''),       isbn),
            edition        = COALESCE(NULLIF(:edition,''),    edition),
            subjects       = COALESCE(NULLIF(:subjects,''),   subjects),
            description    = COALESCE(NULLIF(:description,''),description),
            call_number    = COALESCE(NULLIF(:call_number,''),call_number),
            detail_scraped = 1,
            scraped_at     = datetime('now')
        WHERE biblio_id = :biblio_id
        """,
        {**details, "biblio_id": biblio_id},
    )


def log_page(conn: sqlite3.Connection, page: int, offset: int,
             status: str, records: int) -> None:
    conn.execute(
        "INSERT INTO scrape_log (page, offset, status, records) "
        "VALUES (?, ?, ?, ?)",
        (page, offset, status, records),
    )
    conn.commit()


def get_resume_offset(conn: sqlite3.Connection) -> int:
    """
    Return the offset to resume from.
    Finds the highest offset that was logged as 'ok', then advances by
    one page to avoid re-scraping the last successful page.
    Returns 0 if no successful pages exist yet.
    """
    row = conn.execute(
        "SELECT MAX(offset) FROM scrape_log WHERE status = 'ok'"
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0]) + RESULTS_PER_PAGE
    return 0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch_page(session: requests.Session, url: str) -> requests.Response | None:
    """Fetch a URL with retry logic and exponential back-off."""
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


def build_page_url(base_url: str, offset: int) -> str:
    """Append the Koha offset parameter to the search URL."""
    if offset == 0:
        return base_url
    return f"{base_url}&offset={offset}"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _clean(text: str | None) -> str:
    """Strip and collapse whitespace; return '' for None."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def parse_results_page(html: str, base_url: str) -> tuple[list[dict], int]:
    """
    Parse a Koha OPAC search-results page.

    Returns:
        (records, total_results)
        records       – list of book dicts
        total_results – total hit count advertised by Koha (0 if not found)
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    # ---- Total result count -------------------------------------------------
    total_results = 0
    for candidate in soup.find_all(
        string=re.compile(r"\d[\d,]*\s+result", re.IGNORECASE)
    ):
        m = re.search(r"([\d,]+)\s+result", candidate, re.IGNORECASE)
        if m:
            total_results = int(m.group(1).replace(",", ""))
            break

    if total_results == 0:
        for sel in ("#numresults", ".numresults", "#result-count", ".result-count"):
            el = soup.select_one(sel)
            if el:
                m = re.search(r"([\d,]+)", el.get_text())
                if m:
                    total_results = int(m.group(1).replace(",", ""))
                    break

    # ---- Result items -------------------------------------------------------
    result_items = soup.select("li.search-result, div.search-result")
    if not result_items:
        result_items = soup.select(
            "table#searchresults tr.even, table#searchresults tr.odd"
        )

    for item in result_items:
        record = _parse_result_item(item, base_url)
        if record:
            records.append(record)

    return records, total_results


def _parse_result_item(item, base_url: str) -> dict | None:
    """Extract metadata from a single search-result element."""
    record: dict = {
        "biblio_id":   "",
        "title":       "",
        "author":      "",
        "publisher":   "",
        "year":        "",
        "isbn":        "",
        "edition":     "",
        "item_type":   "",
        "location":    "",
        "call_number": "",
        "subjects":    "",
        "description": "",
        "url":         "",
    }

    # Biblio ID & detail URL
    link_el = item.find("a", href=re.compile(r"biblionumber=(\d+)"))
    if link_el:
        m = re.search(r"biblionumber=(\d+)", link_el["href"])
        if m:
            record["biblio_id"] = m.group(1)
            record["url"] = urljoin(base_url, link_el["href"])

    if not record["biblio_id"]:
        return None

    # Title
    title_el = (
        item.find(class_=re.compile(r"title", re.I))
        or item.find("a", href=re.compile(r"biblionumber="))
    )
    if title_el:
        record["title"] = _clean(title_el.get_text())

    # Author
    author_el = item.find(class_=re.compile(r"author", re.I))
    if author_el:
        record["author"] = _clean(author_el.get_text())

    # Publisher / year
    pub_el = item.find(class_=re.compile(r"publisher|pubdate|imprint", re.I))
    if pub_el:
        pub_text = _clean(pub_el.get_text())
        m_year = re.search(r"\b(1[89]\d{2}|20\d{2})\b", pub_text)
        if m_year:
            record["year"] = m_year.group(1)
        record["publisher"] = pub_text
    else:
        full_text = _clean(item.get_text(" "))
        m_year = re.search(r"\b(1[89]\d{2}|20\d{2})\b", full_text)
        if m_year:
            record["year"] = m_year.group(1)

    # ISBN
    isbn_el = item.find(class_=re.compile(r"isbn", re.I))
    if isbn_el:
        record["isbn"] = _clean(isbn_el.get_text())
    else:
        m_isbn = re.search(r"ISBN[:\s]*([\d\-X]{10,17})", item.get_text(), re.I)
        if m_isbn:
            record["isbn"] = m_isbn.group(1).strip()

    # Call number / location
    call_el = item.find(class_=re.compile(r"call.?number|ccode|location", re.I))
    if call_el:
        record["call_number"] = _clean(call_el.get_text())

    # Item type
    type_el = item.find(class_=re.compile(r"item.?type|itype", re.I))
    if type_el:
        record["item_type"] = _clean(type_el.get_text())

    return record


def parse_detail_page(html: str) -> dict:
    """
    Parse a Koha OPAC opac-detail.pl page for richer metadata.
    Returns a dict with keys: publisher, year, isbn, edition,
    subjects, description, call_number.
    """
    soup = BeautifulSoup(html, "lxml")
    details: dict = {
        "publisher":   "",
        "year":        "",
        "isbn":        "",
        "edition":     "",
        "subjects":    "",
        "description": "",
        "call_number": "",
    }

    # Koha detail pages contain a <div id="catalogue_detail_biblio"> with
    # labeled rows in a <table> or <dl>.

    def _label_text(label_pattern: str) -> str:
        """Find the value next to a label matching the pattern."""
        for el in soup.find_all(string=re.compile(label_pattern, re.I)):
            parent = el.parent
            sibling = parent.find_next_sibling()
            if sibling:
                return _clean(sibling.get_text())
            # table row variant
            td = parent.find_next("td")
            if td:
                return _clean(td.get_text())
        return ""

    # Publisher / publication info
    pub_text = _label_text(r"publicaci[oó]n|publisher|editorial|imprint")
    if pub_text:
        details["publisher"] = pub_text
        m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", pub_text)
        if m:
            details["year"] = m.group(1)

    # Edition
    details["edition"] = _label_text(r"edici[oó]n|edition")

    # ISBN
    isbn_text = _label_text(r"ISBN")
    if not isbn_text:
        m = re.search(r"ISBN[:\s]*([\d\-X ]+)", soup.get_text(), re.I)
        if m:
            isbn_text = m.group(1).strip()
    details["isbn"] = isbn_text

    # Subjects
    subject_els = soup.select(
        "span.subject, a.subject, td.subject, .subjects a, #subject a"
    )
    if subject_els:
        details["subjects"] = "; ".join(
            _clean(s.get_text()) for s in subject_els if _clean(s.get_text())
        )
    else:
        details["subjects"] = _label_text(r"materia|subject|tema")

    # Description / summary / abstract
    for sel in (".description", "#summary", ".summary", "#abstract"):
        el = soup.select_one(sel)
        if el:
            details["description"] = _clean(el.get_text())
            break
    if not details["description"]:
        details["description"] = _label_text(r"resumen|summary|descripci")

    # Call number
    call_el = soup.select_one(".call_no, .callnumber, span.cn_one")
    if call_el:
        details["call_number"] = _clean(call_el.get_text())
    else:
        details["call_number"] = _label_text(r"signatura|call.?number|n[uú]mero de clase")

    return details


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

EXPORT_COLUMNS = [
    "id", "biblio_id", "title", "author", "publisher", "year",
    "isbn", "edition", "item_type", "location", "call_number",
    "subjects", "description", "url", "scraped_at",
]


def export_csv(conn: sqlite3.Connection, csv_path: str) -> int:
    """Write all books to a CSV file. Returns the number of rows written."""
    rows = conn.execute(
        f"SELECT {', '.join(EXPORT_COLUMNS)} FROM books ORDER BY id"
    ).fetchall()
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(EXPORT_COLUMNS)
        writer.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Scraping loops
# ---------------------------------------------------------------------------


def scrape_all(conn: sqlite3.Connection, base_url: str,
               delay: float, limit: int, resume: bool) -> int:
    """
    Paginate through all search results and store records in the DB.
    Returns the total number of records inserted/updated.
    """
    session = requests.Session()
    total_scraped = 0
    total_results = None

    start_offset = get_resume_offset(conn) if resume else 0
    if resume and start_offset > 0:
        log.info("Resuming from offset %d", start_offset)

    offset   = start_offset
    page_num = (start_offset // RESULTS_PER_PAGE) + 1

    while True:
        url = build_page_url(base_url, offset)
        log.info("Fetching page %d (offset=%d) …", page_num, offset)

        resp = fetch_page(session, url)
        if resp is None:
            log_page(conn, page_num, offset, "error", 0)
            log.error("Skipping page %d due to repeated failures.", page_num)
            offset   += RESULTS_PER_PAGE
            page_num += 1
            if total_results and offset >= total_results:
                break
            if offset >= MAX_RESULTS:
                break
            continue

        records, discovered_total = parse_results_page(resp.text, resp.url)

        if total_results is None and discovered_total > 0:
            total_results = discovered_total
            log.info("Total results reported by server: %d", total_results)

        if not records:
            log.warning(
                "No records on page %d (offset=%d) – ending scrape.",
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
            "  → %d records  (total so far: %d / %s)",
            len(records),
            total_scraped,
            total_results if total_results else "?",
        )

        offset   += RESULTS_PER_PAGE
        page_num += 1

        if limit and total_scraped >= limit:
            log.info("Reached requested limit of %d records.", limit)
            break
        if total_results and offset >= total_results:
            log.info("Reached end of results (%d).", total_results)
            break
        if offset >= MAX_RESULTS:
            log.warning("Hit safety cap of %d records.", MAX_RESULTS)
            break

        time.sleep(delay)

    return total_scraped


def scrape_details(conn: sqlite3.Connection, session: requests.Session,
                   delay: float) -> int:
    """
    For every book that has not yet been detail-scraped, fetch its detail
    page and enrich the DB row.  Returns number of books enriched.
    """
    rows = conn.execute(
        "SELECT biblio_id, url FROM books WHERE detail_scraped = 0 AND url != ''"
    ).fetchall()

    enriched = 0
    total = len(rows)
    log.info("Detail-scraping %d books …", total)

    for i, (biblio_id, url) in enumerate(rows, 1):
        log.info("  Detail %d/%d  biblio_id=%s", i, total, biblio_id)
        resp = fetch_page(session, url)
        if resp is None:
            log.warning("  Skipping detail for biblio_id=%s", biblio_id)
            continue
        details = parse_detail_page(resp.text)
        update_book_details(conn, biblio_id, details)
        conn.commit()
        enriched += 1
        time.sleep(delay)

    return enriched


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Scrape the Biblioteca Universidad Wiener OPAC and store "
            "records in a SQLite database."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--db",
        default=DEFAULT_DB,
        metavar="PATH",
        help="SQLite database file to write to",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        metavar="SECONDS",
        help="Seconds to wait between list-page requests",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume an interrupted scrape: skip pages already in scrape_log "
            "with status='ok'"
        ),
    )
    p.add_argument(
        "--count",
        type=int,
        default=0,
        metavar="N",
        help="Stop after collecting N records (0 = no limit)",
    )
    p.add_argument(
        "--details",
        action="store_true",
        help=(
            "After the list-page scrape, visit each book's detail page "
            "to enrich the data (subjects, description, full publisher info). "
            "Significantly slower."
        ),
    )
    p.add_argument(
        "--detail-delay",
        type=float,
        default=DEFAULT_DETAIL_DELAY,
        dest="detail_delay",
        metavar="SECONDS",
        help="Seconds to wait between detail-page requests",
    )
    p.add_argument(
        "--csv",
        metavar="PATH",
        help="Export the current database contents to a CSV file and exit",
    )
    p.add_argument(
        "--url",
        default=DEFAULT_BASE_URL,
        metavar="URL",
        help="Base search URL to scrape (advanced)",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)

    log.info("=== Biblioteca Wiener OPAC Scraper ===")
    conn = init_db(args.db)

    try:
        # ---- CSV export only ------------------------------------------------
        if args.csv:
            n = export_csv(conn, args.csv)
            log.info("Exported %d records to %s", n, args.csv)
            return

        # ---- List-page scrape -----------------------------------------------
        log.info("Database : %s", args.db)
        log.info("Base URL : %s", args.url)
        if args.resume:
            log.info("Mode     : resume")
        if args.count:
            log.info("Limit    : %d records", args.count)

        total = scrape_all(
            conn,
            base_url=args.url,
            delay=args.delay,
            limit=args.count,
            resume=args.resume,
        )
        log.info("List-page scrape complete. Records stored: %d", total)

        # ---- Detail-page enrichment (optional) ------------------------------
        if args.details:
            session = requests.Session()
            enriched = scrape_details(conn, session, args.detail_delay)
            log.info("Detail enrichment complete. Books enriched: %d", enriched)

        # ---- Summary --------------------------------------------------------
        row = conn.execute("SELECT COUNT(*) FROM books").fetchone()
        log.info("Total records in database: %d", row[0])

    finally:
        conn.close()


if __name__ == "__main__":
    main()
