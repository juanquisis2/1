"""
Unit tests for scraper.py – run with:  python -m pytest tests.py -v
"""

import sqlite3
import textwrap
import unittest

from scraper import (
    _clean,
    init_db,
    upsert_book,
    parse_results_page,
    build_page_url,
    BASE_URL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_koha_html(records: list[dict], total: int = 100) -> str:
    """Build a minimal but realistic Koha OPAC search-results HTML page."""
    items_html = ""
    for r in records:
        items_html += textwrap.dedent(f"""
            <li class="search-result">
              <div class="title">
                <a href="/cgi-bin/koha/opac-detail.pl?biblionumber={r['biblio_id']}">
                  {r['title']}
                </a>
              </div>
              <div class="author">{r.get('author', '')}</div>
              <div class="publisher">{r.get('publisher', '')} {r.get('year', '')}</div>
            </li>
        """)

    return textwrap.dedent(f"""
        <!DOCTYPE html>
        <html>
        <body>
          <div id="search-results">
            <p>Results 1-20 of {total} results found</p>
            <ul id="bookbag_form">
              {items_html}
            </ul>
          </div>
        </body>
        </html>
    """)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClean(unittest.TestCase):
    def test_strips_whitespace(self):
        self.assertEqual(_clean("  hello   world  "), "hello world")

    def test_collapses_tabs_newlines(self):
        self.assertEqual(_clean("a\n  b\t  c"), "a b c")

    def test_none_returns_empty(self):
        self.assertEqual(_clean(None), "")

    def test_empty_returns_empty(self):
        self.assertEqual(_clean(""), "")


class TestBuildPageUrl(unittest.TestCase):
    def test_offset_zero_returns_base(self):
        self.assertEqual(build_page_url(0), BASE_URL)

    def test_positive_offset_appended(self):
        url = build_page_url(20)
        self.assertIn("offset=20", url)

    def test_large_offset(self):
        url = build_page_url(13940)
        self.assertIn("offset=13940", url)


class TestInitDb(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_books_table_exists(self):
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("books", tables)

    def test_scrape_log_table_exists(self):
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("scrape_log", tables)


class TestUpsertBook(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _sample(self, biblio_id="42", title="Test Book"):
        return {
            "biblio_id": biblio_id,
            "title": title,
            "author": "Test Author",
            "publisher": "Test Publisher",
            "year": "2020",
            "isbn": "978-0-000000-00-0",
            "edition": "1st",
            "item_type": "BK",
            "location": "Main",
            "call_number": "Z999",
            "url": "https://example.com/detail?biblionumber=42",
        }

    def test_insert_new_record(self):
        upsert_book(self.conn, self._sample())
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        self.assertEqual(count, 1)

    def test_upsert_updates_existing(self):
        upsert_book(self.conn, self._sample(title="Old Title"))
        self.conn.commit()
        upsert_book(self.conn, self._sample(title="New Title"))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT title FROM books WHERE biblio_id='42'"
        ).fetchone()
        self.assertEqual(row[0], "New Title")
        count = self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        self.assertEqual(count, 1)  # no duplicate

    def test_multiple_different_records(self):
        for i in range(5):
            upsert_book(self.conn, self._sample(biblio_id=str(i), title=f"Book {i}"))
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        self.assertEqual(count, 5)


class TestParseResultsPage(unittest.TestCase):
    def _records(self):
        return [
            {"biblio_id": "101", "title": "Python Programming",
             "author": "John Doe", "publisher": "TechPress", "year": "2019"},
            {"biblio_id": "202", "title": "Data Science Handbook",
             "author": "Jane Smith", "publisher": "DataBooks", "year": "2021"},
        ]

    def test_extracts_all_records(self):
        html = _make_koha_html(self._records(), total=500)
        records, total = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        self.assertEqual(len(records), 2)

    def test_total_results_parsed(self):
        html = _make_koha_html(self._records(), total=13950)
        _, total = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        self.assertEqual(total, 13950)

    def test_biblio_id_extracted(self):
        html = _make_koha_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        ids = {r["biblio_id"] for r in records}
        self.assertIn("101", ids)
        self.assertIn("202", ids)

    def test_title_extracted(self):
        html = _make_koha_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        titles = {r["title"] for r in records}
        self.assertIn("Python Programming", titles)

    def test_author_extracted(self):
        html = _make_koha_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        rec = next(r for r in records if r["biblio_id"] == "101")
        self.assertEqual(rec["author"], "John Doe")

    def test_year_extracted(self):
        html = _make_koha_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        rec = next(r for r in records if r["biblio_id"] == "101")
        self.assertEqual(rec["year"], "2019")

    def test_url_is_absolute(self):
        html = _make_koha_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        for rec in records:
            self.assertTrue(
                rec["url"].startswith("https://"),
                f"URL not absolute: {rec['url']!r}",
            )

    def test_empty_page_returns_zero_records(self):
        html = "<html><body><p>No results found.</p></body></html>"
        records, total = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        self.assertEqual(records, [])
        self.assertEqual(total, 0)

    def test_record_without_biblio_id_skipped(self):
        html = textwrap.dedent("""
            <html><body>
              <p>Results 1-1 of 1 results found</p>
              <ul>
                <li class="search-result">
                  <div class="title"><a href="/no-biblionumber">No ID</a></div>
                </li>
              </ul>
            </body></html>
        """)
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
