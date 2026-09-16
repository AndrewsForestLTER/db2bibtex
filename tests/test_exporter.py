"""Tests for db2bibtex.exporter.

These tests must pass even when pyodbc is "installed but unimportable"
(e.g. GitHub Actions runners with the wheel present but no unixODBC system
library) -- nothing here requires a real pyodbc import to succeed. They must
also pass when the real, gitignored query.sql is absent (e.g. a fresh clone
or CI) -- every test that touches load_query/fetch_rows/run_export's query
handling uses an explicit tmp_path file, never the project's own query.sql.
"""

from __future__ import annotations

import configparser
from unittest.mock import MagicMock

import pytest

from db2bibtex import exporter


# ---------------------------------------------------------------------------
# split_authors
# ---------------------------------------------------------------------------
def test_split_authors_double_slash_delimiter():
    raw = "Cromack, K., Jr.//Delwiche, C. C.//McNabb, D. H."
    assert exporter.split_authors(raw) == "Cromack, K., Jr. and Delwiche, C. C. and McNabb, D. H."


def test_split_authors_semicolon_fallback():
    assert exporter.split_authors("Smith, J.; Doe, A.") == "Smith, J. and Doe, A."


def test_split_authors_newline_fallback():
    assert exporter.split_authors("Smith, J.\nDoe, A.") == "Smith, J. and Doe, A."


def test_split_authors_empty():
    assert exporter.split_authors("") == ""
    assert exporter.split_authors(None) == ""


# ---------------------------------------------------------------------------
# escape_bibtex
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("A & B", r"A \& B"),
        ("50% done", r"50\% done"),
        ("$5", r"\$5"),
        ("#1", r"\#1"),
        ("a_b", r"a\_b"),
        ("{x}", r"\{x\}"),
        ("a~b", r"a\textasciitilde{}b"),
        ("a^b", r"a\textasciicircum{}b"),
    ],
)
def test_escape_bibtex_special_chars(raw, expected):
    assert exporter.escape_bibtex(raw) == expected


def test_escape_bibtex_none():
    assert exporter.escape_bibtex(None) == ""


def test_escape_bibtex_strips_whitespace():
    assert exporter.escape_bibtex("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# guess_entry_type
# ---------------------------------------------------------------------------
def test_guess_entry_type_journal_article():
    assert exporter.guess_entry_type("Journal Article", None, None) == "article"


def test_guess_entry_type_book_section():
    assert exporter.guess_entry_type("Book Section", None, None) == "incollection"


def test_guess_entry_type_conference_paper():
    assert exporter.guess_entry_type("Conference Paper", None, None) == "inproceedings"


def test_guess_entry_type_thesis_default_phd():
    assert exporter.guess_entry_type("Thesis", None, None) == "phdthesis"


def test_guess_entry_type_thesis_masters():
    assert exporter.guess_entry_type("Thesis", None, "Master's Thesis") == "mastersthesis"


def test_guess_entry_type_thesis_masters_case_insensitive():
    assert exporter.guess_entry_type("Dissertation", None, "MASTER of Science") == "mastersthesis"


def test_guess_entry_type_falls_back_to_pub_type():
    assert exporter.guess_entry_type(None, "Report", None) == "techreport"


def test_guess_entry_type_default_misc():
    assert exporter.guess_entry_type("Something Unrecognized", None, None) == "misc"


def test_guess_entry_type_unpublished():
    assert exporter.guess_entry_type("Unpublished Work", None, None) == "unpublished"


# ---------------------------------------------------------------------------
# make_cite_key
# ---------------------------------------------------------------------------
def test_make_cite_key_uses_pub_number():
    used = set()
    key = exporter.make_cite_key("Cromack, K.", "1979", "Prospects", used, pub_number=2135)
    assert key == "AND2135"


def test_make_cite_key_collision_suffix():
    used = {"AND2135"}
    key = exporter.make_cite_key("Cromack, K.", "1979", "Prospects", used, pub_number=2135)
    assert key == "AND2135_2"


def test_make_cite_key_multiple_collisions():
    used = {"AND2135", "AND2135_2"}
    key = exporter.make_cite_key("Cromack, K.", "1979", "Prospects", used, pub_number=2135)
    assert key == "AND2135_3"


def test_make_cite_key_fallback_no_pub_number():
    used = set()
    key = exporter.make_cite_key("Smith, John", "2001", "A Study of Forests", used, pub_number=None)
    assert key == "Smith2001A"


def test_make_cite_key_fallback_unknown_author():
    used = set()
    key = exporter.make_cite_key(None, "2001", "A Study", used, pub_number=None)
    assert key.startswith("Unknown2001")


# ---------------------------------------------------------------------------
# build_bibtex_entry -- end to end using the real AND2135 example
# ---------------------------------------------------------------------------
@pytest.fixture
def and2135_row():
    return {
        "publication_id": 2075,
        "catalog_id": 501,
        "pub_number": 2135,
        "pub_status": "Published",
        "pub_type": "Book Section",
        "reference_type": "Book Section",
        "author": "Cromack, K., Jr.//Delwiche, C. C.//McNabb, D. H.",
        "pub_year": "1979",
        "title": "Prospects and Problems of Nitrogen Management Using Symbiotic Nitrogen Fixers",
        "secondary_author": None,
        "secondary_title": None,
        "place_published": None,
        "publisher": None,
        "volume": None,
        "extent": None,
        "issue": None,
        "number_of_pages": None,
        "pages": None,
        "doi": None,
        "news_section": None,
        "tertiary_author": None,
        "tertiary_title": None,
        "edition": None,
        "meeting_date": None,
        "type_of_work": None,
        "subsidiary_author": None,
        "isbn_issn": None,
        "author_role": None,
        "availability": None,
        "meeting_place": None,
        "notes": None,
        "online_linkage": None,
        "online_pdf": "http://andrewsforest.oregonstate.edu/pubs/pdf/pub2135.pdf",
        "pdf": None,
        "abstract": None,
        "citation": None,
        "last_update": None,
    }


def test_build_bibtex_entry_and2135(and2135_row):
    entry = exporter.build_bibtex_entry(and2135_row, used_keys=set())

    assert entry.startswith("@incollection{AND2135,")
    assert "author = {Cromack, K., Jr. and Delwiche, C. C. and McNabb, D. H.}" in entry
    assert "pdf = {http://andrewsforest.oregonstate.edu/pubs/pdf/pub2135.pdf}" in entry
    assert r"keywords = {pub\_number:2135}" in entry
    assert "year = {1979}" in entry
    assert r"publication\_id 2075" in entry
    assert r"pub\_number 2135" in entry
    assert r"catalog\_id 501" in entry


def test_build_bibtex_entry_skips_empty_fields(and2135_row):
    entry = exporter.build_bibtex_entry(and2135_row, used_keys=set())
    assert "doi = " not in entry
    assert "volume = " not in entry


def test_build_bibtex_entry_url_and_pdf_separate(and2135_row):
    row = dict(and2135_row)
    row["online_linkage"] = "https://example.org/landing"
    entry = exporter.build_bibtex_entry(row, used_keys=set())
    assert "url = {https://example.org/landing}" in entry
    assert "pdf = {http://andrewsforest.oregonstate.edu/pubs/pdf/pub2135.pdf}" in entry


def test_build_bibtex_entry_citation_key_collision(and2135_row):
    used = {"AND2135"}
    entry = exporter.build_bibtex_entry(and2135_row, used_keys=used)
    assert entry.startswith("@incollection{AND2135_2,")


# ---------------------------------------------------------------------------
# load_config / save_config round trip
# ---------------------------------------------------------------------------
def test_config_round_trip(tmp_path):
    config_path = tmp_path / "db_config.ini"
    exporter.save_config(
        config_path,
        server="testserver.example.edu",
        database="testdb",
        driver="ODBC Driver 18 for SQL Server",
        uid="myuser",
        pwd="mypassword",
        trust_server_certificate=True,
    )

    cfg = exporter.load_config(config_path)
    assert cfg["server"] == "testserver.example.edu"
    assert cfg["database"] == "testdb"
    assert cfg["driver"] == "ODBC Driver 18 for SQL Server"
    assert cfg["uid"] == "myuser"
    assert cfg["pwd"] == "mypassword"
    assert cfg["trust_server_certificate"] is True


def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        exporter.load_config(tmp_path / "does_not_exist.ini")


def test_load_config_matches_example_format(tmp_path):
    """Mirrors the exact db_config.ini.example shape shipped with the project."""
    config_path = tmp_path / "db_config.ini"
    cp = configparser.ConfigParser()
    cp["Database"] = {
        "server": "testserver.example.edu",
        "database": "testdb",
        "username": "your_username_here",
        "password": "your_password_here",
        "trustservercertificate": "yes",
    }
    cp["Driver"] = {"driver": "{ODBC Driver 18 for SQL Server}"}
    with open(config_path, "w", encoding="utf-8") as fh:
        cp.write(fh)

    cfg = exporter.load_config(config_path)
    assert cfg["driver"] == "ODBC Driver 18 for SQL Server"
    assert cfg["trust_server_certificate"] is True


# ---------------------------------------------------------------------------
# pyodbc-missing behavior (simulates "installed but unimportable")
# ---------------------------------------------------------------------------
def test_fetch_rows_raises_pyodbc_missing_error(monkeypatch, tmp_path):
    monkeypatch.setattr(exporter, "pyodbc", None)
    with pytest.raises(exporter.PyodbcMissingError):
        # query_file doesn't need to exist -- the pyodbc check happens
        # before the query file is ever read.
        exporter.fetch_rows("server", "db", 1980, tmp_path / "unused_query.sql")


def test_run_export_surfaces_pyodbc_missing_error(monkeypatch, tmp_path):
    monkeypatch.setattr(exporter, "pyodbc", None)
    with pytest.raises(exporter.PyodbcMissingError):
        exporter.run_export(
            server="server",
            database="db",
            before_year=1980,
            output_path=tmp_path / "out.bib",
            query_file=tmp_path / "unused_query.sql",
        )


# ---------------------------------------------------------------------------
# load_query / query.sql handling (query.sql itself is gitignored -- these
# tests always use an explicit tmp_path file, never the real project file,
# so they pass whether or not query.sql exists in the working copy)
# ---------------------------------------------------------------------------
def test_load_query_reads_file(tmp_path):
    query_path = tmp_path / "query.sql"
    query_path.write_text("SELECT 1 WHERE x < ?", encoding="utf-8")
    assert exporter.load_query(query_path) == "SELECT 1 WHERE x < ?"


def test_load_query_missing_file_has_actionable_message(tmp_path):
    missing_path = tmp_path / "does_not_exist.sql"
    with pytest.raises(exporter.QueryFileMissingError) as exc_info:
        exporter.load_query(missing_path)
    message = str(exc_info.value)
    assert "query.sql.example" in message
    assert str(missing_path) in message


def test_query_file_missing_error_is_file_not_found_error():
    assert issubclass(exporter.QueryFileMissingError, FileNotFoundError)


# ---------------------------------------------------------------------------
# No baked-in database name or query-file path: the public codebase must not
# silently default to any particular institution's database name or query
# file location -- every caller has to be explicit.
# ---------------------------------------------------------------------------
def test_no_default_query_file_constant_exposed():
    assert not hasattr(exporter, "DEFAULT_QUERY_FILE")


def test_load_query_has_no_default_argument():
    with pytest.raises(TypeError):
        exporter.load_query()


def test_fetch_rows_requires_query_file_argument():
    with pytest.raises(TypeError):
        exporter.fetch_rows("server", "db", 1980)


def test_run_export_requires_query_file_argument(tmp_path):
    with pytest.raises(TypeError):
        exporter.run_export(
            server="server",
            database="db",
            before_year=1980,
            output_path=tmp_path / "out.bib",
        )


def test_load_config_has_no_default_database(tmp_path):
    """load_config() must not silently fall back to any particular
    database name when the config file omits one."""
    config_path = tmp_path / "db_config.ini"
    cp = configparser.ConfigParser()
    cp["Database"] = {"server": "testserver.example.edu"}  # no "database" key
    with open(config_path, "w", encoding="utf-8") as fh:
        cp.write(fh)

    cfg = exporter.load_config(config_path)
    assert cfg["database"] is None


def test_fetch_rows_raises_query_file_missing_error(monkeypatch, tmp_path):
    # pyodbc must appear available so the code reaches the query-file check
    # instead of short-circuiting on PyodbcMissingError first.
    monkeypatch.setattr(exporter, "pyodbc", MagicMock())
    missing_path = tmp_path / "query.sql"
    with pytest.raises(exporter.QueryFileMissingError):
        exporter.fetch_rows("server", "db", 1980, query_file=missing_path)


def test_run_export_surfaces_query_file_missing_error(monkeypatch, tmp_path):
    monkeypatch.setattr(exporter, "pyodbc", MagicMock())
    missing_path = tmp_path / "query.sql"
    with pytest.raises(exporter.QueryFileMissingError):
        exporter.run_export(
            server="server",
            database="db",
            before_year=1980,
            output_path=tmp_path / "out.bib",
            query_file=missing_path,
        )
