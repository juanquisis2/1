"""
Unit tests for scraper.py – run with:  python -m pytest tests.py -v
"""

import csv
import io
import sqlite3
import sys
import textwrap
import unittest
from unittest.mock import MagicMock, patch

from scraper import (
    _clean,
    build_arg_parser,
    build_page_url,
    export_csv,
    get_resume_offset,
    init_db,
    log_page,
    parse_detail_page,
    parse_results_page,
    upsert_book,
    update_book_details,
    DEFAULT_BASE_URL,
    RESULTS_PER_PAGE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_record(biblio_id="42", title="Test Book"):
    return {
        "biblio_id":   biblio_id,
        "title":       title,
        "author":      "Test Author",
        "publisher":   "Test Publisher",
        "year":        "2020",
        "isbn":        "978-0-000000-00-0",
        "edition":     "1st",
        "item_type":   "BK",
        "location":    "Main",
        "call_number": "Z999",
        "subjects":    "",
        "description": "",
        "url":         f"https://example.com/detail?biblionumber={biblio_id}",
    }


def _make_results_html(records: list[dict], total: int = 100) -> str:
    """Build a minimal Koha OPAC search-results HTML page."""
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
        <!DOCTYPE html><html><body>
          <p>Results 1-20 of {total} results found</p>
          <ul id="bookbag_form">{items_html}</ul>
        </body></html>
    """)


def _make_detail_html(publisher="Pearson", year="2019",
                      isbn="978-0-13-110362-7",
                      edition="3rd ed.",
                      subjects="Python; Programming",
                      description="A great book about Python.") -> str:
    return textwrap.dedent(f"""
        <!DOCTYPE html><html><body>
          <table>
            <tr><td>Publicación</td><td>{publisher}, {year}</td></tr>
            <tr><td>Edición</td><td>{edition}</td></tr>
            <tr><td>ISBN</td><td>{isbn}</td></tr>
          </table>
          <div class="subjects">
            <a class="subject">Python</a>
            <a class="subject">Programming</a>
          </div>
          <div class="description">{description}</div>
        </body></html>
    """)


# ---------------------------------------------------------------------------
# _clean
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


# ---------------------------------------------------------------------------
# build_page_url
# ---------------------------------------------------------------------------

class TestBuildPageUrl(unittest.TestCase):
    def test_offset_zero_returns_base(self):
        self.assertEqual(build_page_url(DEFAULT_BASE_URL, 0), DEFAULT_BASE_URL)

    def test_positive_offset_appended(self):
        url = build_page_url(DEFAULT_BASE_URL, 20)
        self.assertIn("offset=20", url)

    def test_large_offset(self):
        url = build_page_url(DEFAULT_BASE_URL, 13940)
        self.assertIn("offset=13940", url)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_books_table_exists(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("books", tables)

    def test_scrape_log_table_exists(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("scrape_log", tables)

    def test_books_has_subjects_column(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(books)")}
        self.assertIn("subjects", cols)

    def test_books_has_description_column(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(books)")}
        self.assertIn("description", cols)

    def test_books_has_detail_scraped_column(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(books)")}
        self.assertIn("detail_scraped", cols)


# ---------------------------------------------------------------------------
# upsert_book
# ---------------------------------------------------------------------------

class TestUpsertBook(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_insert_new_record(self):
        upsert_book(self.conn, _sample_record())
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0], 1
        )

    def test_upsert_updates_existing(self):
        upsert_book(self.conn, _sample_record(title="Old Title"))
        self.conn.commit()
        upsert_book(self.conn, _sample_record(title="New Title"))
        self.conn.commit()
        row = self.conn.execute(
            "SELECT title FROM books WHERE biblio_id='42'"
        ).fetchone()
        self.assertEqual(row[0], "New Title")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0], 1
        )

    def test_multiple_different_records(self):
        for i in range(5):
            upsert_book(self.conn, _sample_record(biblio_id=str(i)))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0], 5
        )


# ---------------------------------------------------------------------------
# update_book_details
# ---------------------------------------------------------------------------

class TestUpdateBookDetails(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        upsert_book(self.conn, _sample_record())
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_updates_subjects(self):
        update_book_details(self.conn, "42", {
            "publisher": "", "year": "", "isbn": "",
            "edition": "", "subjects": "Python; Testing",
            "description": "", "call_number": "",
        })
        self.conn.commit()
        row = self.conn.execute(
            "SELECT subjects FROM books WHERE biblio_id='42'"
        ).fetchone()
        self.assertEqual(row[0], "Python; Testing")

    def test_sets_detail_scraped_flag(self):
        update_book_details(self.conn, "42", {
            "publisher": "", "year": "", "isbn": "",
            "edition": "", "subjects": "",
            "description": "Some description", "call_number": "",
        })
        self.conn.commit()
        row = self.conn.execute(
            "SELECT detail_scraped FROM books WHERE biblio_id='42'"
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_does_not_overwrite_existing_value_with_empty(self):
        """COALESCE logic: non-empty original should survive empty patch."""
        update_book_details(self.conn, "42", {
            "publisher": "", "year": "", "isbn": "",
            "edition": "", "subjects": "",
            "description": "", "call_number": "",
        })
        self.conn.commit()
        row = self.conn.execute(
            "SELECT publisher FROM books WHERE biblio_id='42'"
        ).fetchone()
        self.assertEqual(row[0], "Test Publisher")


# ---------------------------------------------------------------------------
# get_resume_offset
# ---------------------------------------------------------------------------

class TestGetResumeOffset(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_returns_zero_when_no_log(self):
        self.assertEqual(get_resume_offset(self.conn), 0)

    def test_returns_next_page_after_last_ok(self):
        log_page(self.conn, 1, 0, "ok", 20)
        log_page(self.conn, 2, 20, "ok", 20)
        log_page(self.conn, 3, 40, "error", 0)
        # Last OK offset is 20 → resume at 20 + RESULTS_PER_PAGE = 40
        self.assertEqual(get_resume_offset(self.conn), 40)

    def test_ignores_error_entries(self):
        log_page(self.conn, 1, 0, "error", 0)
        self.assertEqual(get_resume_offset(self.conn), 0)


# ---------------------------------------------------------------------------
# parse_results_page
# ---------------------------------------------------------------------------

class TestParseResultsPage(unittest.TestCase):
    def _records(self):
        return [
            {"biblio_id": "101", "title": "Python Programming",
             "author": "John Doe", "publisher": "TechPress", "year": "2019"},
            {"biblio_id": "202", "title": "Data Science Handbook",
             "author": "Jane Smith", "publisher": "DataBooks", "year": "2021"},
        ]

    def test_extracts_all_records(self):
        html = _make_results_html(self._records(), total=500)
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        self.assertEqual(len(records), 2)

    def test_total_results_parsed(self):
        html = _make_results_html(self._records(), total=13950)
        _, total = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        self.assertEqual(total, 13950)

    def test_biblio_id_extracted(self):
        html = _make_results_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        ids = {r["biblio_id"] for r in records}
        self.assertIn("101", ids)
        self.assertIn("202", ids)

    def test_title_extracted(self):
        html = _make_results_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        titles = {r["title"] for r in records}
        self.assertIn("Python Programming", titles)

    def test_author_extracted(self):
        html = _make_results_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        rec = next(r for r in records if r["biblio_id"] == "101")
        self.assertEqual(rec["author"], "John Doe")

    def test_year_extracted(self):
        html = _make_results_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        rec = next(r for r in records if r["biblio_id"] == "101")
        self.assertEqual(rec["year"], "2019")

    def test_url_is_absolute(self):
        html = _make_results_html(self._records())
        records, _ = parse_results_page(html, "https://biblioteca.uwiener.edu.pe")
        for rec in records:
            self.assertTrue(rec["url"].startswith("https://"),
                            f"URL not absolute: {rec['url']!r}")

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


# ---------------------------------------------------------------------------
# parse_detail_page
# ---------------------------------------------------------------------------

class TestParseDetailPage(unittest.TestCase):
    def test_subjects_extracted(self):
        html = _make_detail_html()
        details = parse_detail_page(html)
        self.assertIn("Python", details["subjects"])
        self.assertIn("Programming", details["subjects"])

    def test_description_extracted(self):
        html = _make_detail_html(description="A great book about Python.")
        details = parse_detail_page(html)
        self.assertEqual(details["description"], "A great book about Python.")

    def test_year_extracted_from_publisher_field(self):
        html = _make_detail_html(publisher="Pearson", year="2019")
        details = parse_detail_page(html)
        self.assertEqual(details["year"], "2019")

    def test_returns_dict_with_required_keys(self):
        html = _make_detail_html()
        details = parse_detail_page(html)
        for key in ("publisher", "year", "isbn", "edition",
                    "subjects", "description", "call_number"):
            self.assertIn(key, details)


# ---------------------------------------------------------------------------
# export_csv
# ---------------------------------------------------------------------------

class TestExportCsv(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        for i in range(3):
            upsert_book(self.conn, _sample_record(biblio_id=str(i),
                                                  title=f"Book {i}"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_exports_correct_row_count(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            n = export_csv(self.conn, path)
            self.assertEqual(n, 3)
        finally:
            os.unlink(path)

    def test_csv_has_header_and_data_rows(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False, mode="w"
        ) as f:
            path = f.name
        try:
            export_csv(self.conn, path)
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0][0], "id")         # header
            self.assertEqual(len(rows), 4)              # 1 header + 3 data
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# CLI / argparse
# ---------------------------------------------------------------------------

class TestArgParser(unittest.TestCase):
    def test_defaults(self):
        p = build_arg_parser()
        args = p.parse_args([])
        self.assertEqual(args.db, "biblioteca_wiener.db")
        self.assertEqual(args.delay, 1.0)
        self.assertFalse(args.resume)
        self.assertEqual(args.count, 0)
        self.assertFalse(args.details)
        self.assertIsNone(args.csv)

    def test_resume_flag(self):
        args = build_arg_parser().parse_args(["--resume"])
        self.assertTrue(args.resume)

    def test_custom_db(self):
        args = build_arg_parser().parse_args(["--db", "/tmp/test.db"])
        self.assertEqual(args.db, "/tmp/test.db")

    def test_count_option(self):
        args = build_arg_parser().parse_args(["--count", "100"])
        self.assertEqual(args.count, 100)

    def test_csv_option(self):
        args = build_arg_parser().parse_args(["--csv", "out.csv"])
        self.assertEqual(args.csv, "out.csv")

    def test_details_flag(self):
        args = build_arg_parser().parse_args(["--details"])
        self.assertTrue(args.details)

    def test_detail_delay_option(self):
        args = build_arg_parser().parse_args(["--detail-delay", "2.5"])
        self.assertEqual(args.detail_delay, 2.5)


if __name__ == "__main__":
    unittest.main()
