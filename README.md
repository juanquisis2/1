# Biblioteca UWiener Scraper

Scrapes the **~13,950 book records** from the [UWiener library OPAC](https://biblioteca.uwiener.edu.pe/cgi-bin/koha/opac-search.pl?advsearch=1&weight_search=1&limit=mc-itype%2Cphr%3A01&sort_by=relevance&do=Buscar) (Koha system) and stores them in a local SQLite database (`biblioteca.db`).

## Requirements

- Python 3.10+
- Internet access to `biblioteca.uwiener.edu.pe`

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python3 scraper.py
```

The script will:
1. Fetch all pages (20 results per request by default).
2. Parse title, author, publisher, year, ISBN, availability, call number, etc.
3. Save everything to **`biblioteca.db`** (SQLite).
4. Resume automatically if interrupted (already-scraped offsets are skipped).

## Configuration (environment variables)

| Variable     | Default | Description                          |
|--------------|---------|--------------------------------------|
| `PAGE_SIZE`  | `20`    | Results fetched per HTTP request     |
| `DELAY`      | `1.0`   | Seconds to wait between requests     |
| `START_PAGE` | `0`     | Page number to resume from           |

Example — faster scrape with 100 results per page and 0.5 s delay:

```bash
PAGE_SIZE=100 DELAY=0.5 python3 scraper.py
```

## Database schema

```sql
CREATE TABLE books (
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
);
```

A `scrape_log` table also tracks every fetched offset for resumability.

## Querying results

```bash
sqlite3 biblioteca.db "SELECT COUNT(*) FROM books;"
sqlite3 biblioteca.db "SELECT title, author, pub_year FROM books LIMIT 10;"
```
