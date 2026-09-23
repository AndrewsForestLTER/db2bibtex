"""Tests for db2bibtex.db: the format-agnostic core shared by
exporter.py (BibTeX) and ris_export.py (RIS).

These pure-function helpers used to live inline inside exporter.py, where
they were only exercised indirectly through build_bibtex_entry()/
fetch_rows(). They get direct coverage here now that ris_export.py depends
on the exact same behavior.
"""

from __future__ import annotations

import configparser

import pytest

from db2bibtex import db


# ---------------------------------------------------------------------------
# build_conn_str
# ---------------------------------------------------------------------------

def test_build_conn_str_sql_auth():
    conn_str = db.build_conn_str(
        "ODBC Driver 18 for SQL Server", "myserver", "mydb", False, "user1", "pass1"
    )
    assert conn_str == (
        "DRIVER={ODBC Driver 18 for SQL Server};SERVER=myserver;DATABASE=mydb;"
        "UID=user1;PWD=pass1;Encrypt=yes;TrustServerCertificate=yes;"
    )


def test_build_conn_str_trusted_connection_omits_uid_pwd():
    conn_str = db.build_conn_str("ODBC Driver 18 for SQL Server", "myserver", "mydb", True)
    assert "Trusted_Connection=yes" in conn_str
    assert "UID=" not in conn_str
    assert "PWD=" not in conn_str


def test_build_conn_str_encrypt_and_trust_cert_flags():
    conn_str = db.build_conn_str(
        "d", "s", "db", False, "u", "p", encrypt=False, trust_server_certificate=False
    )
    assert "Encrypt=no" in conn_str
    assert "TrustServerCertificate=no" in conn_str


# ---------------------------------------------------------------------------
# load_config / save_config
# ---------------------------------------------------------------------------

def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        db.load_config(tmp_path / "does_not_exist.ini")


def test_save_config_round_trip(tmp_path):
    config_path = tmp_path / "db_config.ini"
    db.save_config(config_path, server="testserver", database="testdb", uid="u", pwd="p")

    result = db.load_config(config_path)

    assert result["server"] == "testserver"
    assert result["database"] == "testdb"
    assert result["uid"] == "u"
    assert result["pwd"] == "p"
    assert result["driver"] == db.DEFAULT_DRIVER


def test_save_config_preserves_other_sections(tmp_path):
    config_path = tmp_path / "db_config.ini"
    cp = configparser.ConfigParser()
    cp["Zotero"] = {"library_id": "12345"}
    with open(config_path, "w") as f:
        cp.write(f)

    db.save_config(config_path, server="testserver", database="testdb")

    result = configparser.ConfigParser()
    result.read(config_path)
    assert result["Zotero"]["library_id"] == "12345"
    assert result["Database"]["server"] == "testserver"


# ---------------------------------------------------------------------------
# load_query
# ---------------------------------------------------------------------------

def test_load_query_missing_file_raises_actionable_error(tmp_path):
    with pytest.raises(db.QueryFileMissingError):
        db.load_query(tmp_path / "query.sql")


def test_load_query_reads_file_contents(tmp_path):
    query_file = tmp_path / "query.sql"
    query_file.write_text("SELECT 1")
    assert db.load_query(query_file) == "SELECT 1"


# ---------------------------------------------------------------------------
# split_authors_list
# ---------------------------------------------------------------------------

def test_split_authors_list_double_slash_delimiter():
    raw = "Cromack, K., Jr.//Delwiche, C. C.//McNabb, D. H."
    assert db.split_authors_list(raw) == ["Cromack, K., Jr.", "Delwiche, C. C.", "McNabb, D. H."]


def test_split_authors_list_semicolon_delimiter():
    assert db.split_authors_list("Smith, J.; Doe, A.") == ["Smith, J.", "Doe, A."]


def test_split_authors_list_empty():
    assert db.split_authors_list(None) == []
    assert db.split_authors_list("") == []


# ---------------------------------------------------------------------------
# match_reference_type
# ---------------------------------------------------------------------------

def test_match_reference_type_first_match_wins_on_insertion_order():
    type_map = {"book section": "CHAP", "book": "BOOK"}
    assert db.match_reference_type("Book Section", None, type_map) == "CHAP"
    assert db.match_reference_type("Book", None, type_map) == "BOOK"


def test_match_reference_type_falls_back_to_pub_type():
    type_map = {"newspaper article": "NEWS"}
    assert db.match_reference_type(None, "Newspaper Article", type_map) == "NEWS"


def test_match_reference_type_no_match_returns_none():
    assert db.match_reference_type("Something Unmapped", None, {"book": "BOOK"}) is None


def test_match_reference_type_case_and_whitespace_insensitive():
    type_map = {"newspaper article": "NEWS"}
    assert db.match_reference_type("  NEWSPAPER ARTICLE  ", None, type_map) == "NEWS"


def test_match_reference_type_does_not_match_mid_word():
    """Found via a real ris_export test failure: a short key like "map"
    must not fire just because it's embedded inside another word."""
    type_map = {"map": "MAP"}
    assert db.match_reference_type("Something Unmapped", None, type_map) is None
    assert db.match_reference_type("Map", None, type_map) == "MAP"


# ---------------------------------------------------------------------------
# resolve_pdf_and_url
# ---------------------------------------------------------------------------

def test_resolve_pdf_and_url_uses_online_pdf_verbatim():
    pdf, url = db.resolve_pdf_and_url(2135, "https://example.org/detail", "https://example.org/x.pdf", None)
    assert pdf == "https://example.org/x.pdf"
    assert url == "https://andrewsforest.oregonstate.edu/publications/2135"


def test_resolve_pdf_and_url_derives_pdf_from_flag():
    pdf, url = db.resolve_pdf_and_url(2135, None, None, "T")
    assert pdf == "https://andrewsforest.oregonstate.edu/pubs/pdf/pub2135.pdf"
    assert url == "https://andrewsforest.oregonstate.edu/publications/2135"


def test_resolve_pdf_and_url_no_pdf_falls_back_to_online_linkage():
    pdf, url = db.resolve_pdf_and_url(None, "https://example.org/detail", None, "F")
    assert pdf is None
    assert url == "https://example.org/detail"


def test_resolve_pdf_and_url_nothing_available():
    pdf, url = db.resolve_pdf_and_url(None, None, None, None)
    assert pdf is None
    assert url is None


def test_resolve_pdf_and_url_pdf_flag_false_values_ignored():
    pdf, url = db.resolve_pdf_and_url(2135, None, None, "F")
    assert pdf is None


# ---------------------------------------------------------------------------
# build_source_note
# ---------------------------------------------------------------------------

def test_build_source_note_without_notes():
    note = db.build_source_note(None, 5378, 5327, 12092)
    assert note == "Source DB: publication_id 5378; pub_number 5327; catalog_id 12092"


def test_build_source_note_with_notes():
    note = db.build_source_note("LTER8-3; HJA4; FY24; media", 5378, 5327, 12092)
    assert note == (
        "Notes: LTER8-3; HJA4; FY24; media; "
        "Source DB: publication_id 5378; pub_number 5327; catalog_id 12092"
    )


# ---------------------------------------------------------------------------
# get_field
# ---------------------------------------------------------------------------

def test_get_field_dict():
    assert db.get_field({"title": "Foo"}, "title") == "Foo"


def test_get_field_object_attribute():
    class Row:
        title = "Bar"

    assert db.get_field(Row(), "title") == "Bar"
