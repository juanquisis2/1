# Biblioteca Wiener OPAC Scraper

Scrapes all bibliographic records (≈ 13 950 books) from the
[Universidad Wiener library catalogue](https://biblioteca.uwiener.edu.pe/cgi-bin/koha/opac-search.pl?advsearch=1&weight_search=1&limit=mc-itype%2Cphr%3A01&sort_by=relevance&do=Buscar)
and stores them in a local **SQLite** database (`biblioteca_wiener.db`).

---

## Requirements

- Python 3.10+
- Internet access to `biblioteca.uwiener.edu.pe`

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python scraper.py
```

The script will:
1. Detect the total number of results from the first page.
2. Paginate through all pages (20 records per page → ~698 pages).
3. Insert/update every record in `biblioteca_wiener.db`.
4. Write a progress log to `scraper.log`.

Estimated run time: **15–30 minutes** (depends on server response time).

---

## Database schema

### `books`

| Column       | Type    | Description                                   |
|--------------|---------|-----------------------------------------------|
| `id`         | INTEGER | Auto-increment primary key                    |
| `biblio_id`  | TEXT    | Koha `biblionumber` (unique)                  |
| `title`      | TEXT    | Book title                                    |
| `author`     | TEXT    | Main author                                   |
| `publisher`  | TEXT    | Publisher / imprint                           |
| `year`       | TEXT    | Publication year                              |
| `isbn`       | TEXT    | ISBN-10 or ISBN-13                            |
| `edition`    | TEXT    | Edition statement                             |
| `item_type`  | TEXT    | Koha item type code                           |
| `location`   | TEXT    | Shelf location                                |
| `call_number`| TEXT    | Call number / classification                  |
| `url`        | TEXT    | Direct link to the OPAC detail page           |
| `scraped_at` | DATETIME| Timestamp of last scrape                      |

### `scrape_log`

Tracks every page request (page number, offset, HTTP status, records parsed).

---

## Querying the data

```bash
sqlite3 biblioteca_wiener.db
```

```sql
-- Total records
SELECT COUNT(*) FROM books;

-- Sample rows
SELECT biblio_id, title, author, year FROM books LIMIT 10;

-- Books by a specific author
SELECT title, year FROM books WHERE author LIKE '%García%';

-- Books published after 2010
SELECT title, author, year FROM books WHERE CAST(year AS INTEGER) > 2010;
```

---

## Configuration

All settings are at the top of `scraper.py`:

| Variable                | Default | Description                                  |
|-------------------------|---------|----------------------------------------------|
| `RESULTS_PER_PAGE`      | 20      | Items per page (Koha default)                |
| `DEFAULT_DELAY`         | 1.0 s   | Polite crawl delay between pages             |
| `MAX_RETRIES`           | 5       | Retry attempts per page on network error     |
| `RETRY_BACKOFF`         | 5 s     | Base back-off between retries                |
| `DEFAULT_DB`            | `biblioteca_wiener.db` | SQLite output file          |
