"""Tests for db2bibtex.ris_export.

These tests must pass even when pyodbc is "installed but unimportable"
(e.g. GitHub Actions runners with the wheel present but no unixODBC system
library) -- nothing here requires a real pyodbc import to succeed, same
convention as test_exporter.py.

Type-code and field-mapping choices in ris_export.py were verified against
Zotero's actual RIS.js translator source (exportTypeMap/importTypeMap and
fieldMap), not just the RIS spec -- see ris_export.py's module docstring.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from db2bibtex import ris_export


# ---------------------------------------------------------------------------
# guess_ris_type -- verified against the 17 real reference_type values
# distinct in the live LTERMETA.dbo.publication table, plus a few kept
# for parity with exporter.ENTRY_TYPE_MAP even though not seen live.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "reference_type,expected",
    [
        ("Journal Article", "JOUR"),
        ("Magazine Article", "MGZN"),
        ("Newspaper Article", "NEWS"),  # the exact bug this exporter exists to fix
        ("Book Section", "CHAP"),
        ("Book Chapter", "CHAP"),
        ("Edited Book", "BOOK"),
        ("Book", "BOOK"),
        ("Conference Paper", "CONF"),
        ("Conference Proceedings", "CONF"),
        ("Edited Conference Proceedings", "CONF"),
        ("Thesis", "THES"),
        ("Dissertation", "THES"),
        ("Report", "RPRT"),
        ("Report Section", "RPRT"),
        ("Government Document", "RPRT"),
        ("Unpublished", "UNPB"),
        ("Manuscript", "MANSCPT"),
        ("Web Page", "ELEC"),
        ("Web Document", "ELEC"),
        ("Electronic Article", "JOUR"),
        ("Audiovisual Material", "ADVS"),
        ("Computer Program", "COMP"),
        ("Map", "MAP"),
    ],
)
def test_guess_ris_type_covers_all_real_reference_type_values(reference_type, expected):
    assert ris_export.guess_ris_type(reference_type, None) == expected


def test_guess_ris_type_case_and_padding_insensitive():
    # LTERMETA's reference_type column has been observed with trailing
    # padding and inconsistent case.
    assert ris_export.guess_ris_type("  NEWSPAPER ARTICLE   ", None) == "NEWS"


def test_guess_ris_type_falls_back_to_pub_type():
    assert ris_export.guess_ris_type(None, "Newspaper Article") == "NEWS"


def test_guess_ris_type_unmapped_defaults_to_gen():
    assert ris_export.guess_ris_type("Something Unmapped", None) == "GEN"


def test_guess_ris_type_none_defaults_to_gen():
    assert ris_export.guess_ris_type(None, None) == "GEN"


# ---------------------------------------------------------------------------
# build_ris_entry
# ---------------------------------------------------------------------------

@pytest.fixture
def mapes_row():
    """Modeled directly on the real Mapes/Seattle Times record (pub_number
    5327) that misclassifies as 'Journal Article' when exported as BibTeX
    -- the reference_type here is exactly what's in the live database."""
    return {
        "publication_id": 5378,
        "catalog_id": 12092,
        "pub_number": 5327,
        "pub_type": "Newspaper Article",
        "reference_type": "Newspaper Article",
        "author": "Mapes, Lynda V.",
        "pub_year": "2023",
        "title": "Decades of research burned in this Oregon forest. Now it could hold clues to wildfire mysteries",
        "secondary_author": None,
        "secondary_title": "The Seattle Times",
        "place_published": "Seattle",
        "publisher": None,
        "volume": None,
        "issue": None,
        "pages": None,
        "doi": None,
        "type_of_work": None,
        "isbn_issn": None,
        "notes": "LTER8-3; HJA4; FY24; media",
        "online_linkage": None,
        "online_pdf": None,
        "pdf": None,
        "abstract": "It was a single lightning strike, deep in the heart of the forest.",
        "url": "https://www.seattletimes.com/seattle-news/environment/decades-of-research",
    }


def test_build_ris_entry_mapes_newspaper_article(mapes_row):
    entry = ris_export.build_ris_entry(mapes_row)

    assert entry.startswith("TY  - NEWS\r\n")
    assert "TY  - JOUR" not in entry  # the whole point
    assert "AU  - Mapes, Lynda V." in entry
    assert "TI  - Decades of research burned in this Oregon forest" in entry
    assert "T2  - The Seattle Times" in entry
    assert "CY  - Seattle" in entry
    assert "PY  - 2023" in entry
    assert "N1  - Notes: LTER8-3; HJA4; FY24; media; Source DB: publication_id 5378; " in entry
    assert "pub_number 5327; catalog_id 12092" in entry
    assert "KW  - pub_number:5327" in entry
    assert entry.rstrip("\r\n").endswith("ER  - ")
    assert entry.endswith("\r\n")


def test_build_ris_entry_skips_empty_fields(mapes_row):
    entry = ris_export.build_ris_entry(mapes_row)
    assert "VL  - " not in entry
    assert "IS  - " not in entry
    assert "DO  - " not in entry
    assert "PB  - " not in entry


def test_build_ris_entry_multiple_authors_one_au_line_each():
    row = dict(_book_section_row())
    row["author"] = "Cromack, K., Jr.//Delwiche, C. C.//McNabb, D. H."
    entry = ris_export.build_ris_entry(row)
    assert "AU  - Cromack, K., Jr." in entry
    assert "AU  - Delwiche, C. C." in entry
    assert "AU  - McNabb, D. H." in entry


def _book_section_row(**overrides):
    row = {
        "publication_id": 2075,
        "catalog_id": 501,
        "pub_number": 2135,
        "pub_type": "Book Section",
        "reference_type": "Book Section",
        "author": "Cromack, K., Jr.",
        "pub_year": "1979",
        "title": "Prospects and Problems of Nitrogen Management",
        "secondary_author": "Gessel, S. P.",
        "secondary_title": "Forests: Fresh Perspectives",
        "place_published": None,
        "publisher": "OSU Press",
        "volume": None,
        "issue": None,
        "pages": "45-67",
        "doi": None,
        "type_of_work": None,
        "isbn_issn": "978-0870713483",
        "notes": None,
        "online_linkage": None,
        "online_pdf": "http://andrewsforest.oregonstate.edu/pubs/pdf/pub2135.pdf",
        "pdf": None,
        "abstract": None,
    }
    row.update(overrides)
    return row


def test_build_ris_entry_book_section_editor_uses_a2():
    entry = ris_export.build_ris_entry(_book_section_row())
    assert "A2  - Gessel, S. P." in entry
    assert "A3  - Gessel, S. P." not in entry
    assert "T2  - Forests: Fresh Perspectives" in entry


def test_build_ris_entry_book_editor_uses_a3():
    row = _book_section_row(
        pub_type="Book", reference_type="Book", secondary_title=None
    )
    entry = ris_export.build_ris_entry(row)
    assert "A3  - Gessel, S. P." in entry
    assert "A2  - Gessel, S. P." not in entry
    # T2 has no meaningful mapping for "book" -- must not be written at all
    assert "T2  - " not in entry


def test_build_ris_entry_pages_split_into_sp_ep():
    entry = ris_export.build_ris_entry(_book_section_row())
    assert "SP  - 45" in entry
    assert "EP  - 67" in entry


def test_build_ris_entry_pages_without_range_goes_to_sp_only():
    row = _book_section_row(pages="99")
    entry = ris_export.build_ris_entry(row)
    assert "SP  - 99" in entry
    assert "EP  - " not in entry


def test_build_ris_entry_pdf_and_url_resolution_matches_shared_helper():
    entry = ris_export.build_ris_entry(_book_section_row())
    assert "L1  - http://andrewsforest.oregonstate.edu/pubs/pdf/pub2135.pdf" in entry
    assert "UR  - https://andrewsforest.oregonstate.edu/publications/2135" in entry


def test_build_ris_entry_isbn_issn_written_as_sn_regardless_of_type():
    entry = ris_export.build_ris_entry(_book_section_row())
    assert "SN  - 978-0870713483" in entry


def test_build_ris_entry_thesis_type_of_work_becomes_m3():
    row = _book_section_row(
        pub_type="Thesis",
        reference_type="Thesis",
        type_of_work="Master's Thesis",
        secondary_title=None,
    )
    entry = ris_export.build_ris_entry(row)
    assert entry.startswith("TY  - THES\r\n")
    assert "M3  - Master's Thesis" in entry


def test_build_ris_entry_m3_omitted_for_non_thesis():
    row = _book_section_row(type_of_work="Master's Thesis")
    entry = ris_export.build_ris_entry(row)
    assert "M3  - " not in entry


def test_build_ris_entry_no_latex_escaping():
    # RIS is plain text -- special characters that BibTeX would escape
    # must survive untouched.
    row = _book_section_row(title="50% growth & other {notes}")
    entry = ris_export.build_ris_entry(row)
    assert "TI  - 50% growth & other {notes}" in entry


# ---------------------------------------------------------------------------
# Missing optional dependencies / query file
# ---------------------------------------------------------------------------

def test_fetch_rows_raises_pyodbc_missing_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ris_export, "pyodbc", None)
    with pytest.raises(ris_export.PyodbcMissingError):
        ris_export.fetch_rows("s", "d", 2026, str(tmp_path / "query.sql"))


def test_run_export_surfaces_pyodbc_missing_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ris_export, "pyodbc", None)
    with pytest.raises(ris_export.PyodbcMissingError):
        ris_export.run_export(
            "s", "d", 2026, str(tmp_path / "out.ris"), str(tmp_path / "query.sql")
        )


def test_fetch_rows_raises_query_file_missing_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ris_export, "pyodbc", MagicMock())
    with pytest.raises(ris_export.QueryFileMissingError):
        ris_export.fetch_rows("s", "d", 2026, str(tmp_path / "does_not_exist.sql"))


# ---------------------------------------------------------------------------
# run_export (pyodbc fully mocked)
# ---------------------------------------------------------------------------

def test_run_export_writes_ris_file_and_skips_missing_title_author(monkeypatch, tmp_path):
    query_file = tmp_path / "query.sql"
    query_file.write_text("SELECT 1")

    good_row = _book_section_row()
    bad_row = dict(_book_section_row())
    bad_row["title"] = None

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [good_row, bad_row]
    mock_conn.cursor.return_value = mock_cursor
    mock_pyodbc = MagicMock()
    mock_pyodbc.connect.return_value = mock_conn
    monkeypatch.setattr(ris_export, "pyodbc", mock_pyodbc)

    output_path = tmp_path / "out.ris"
    messages = []
    result = ris_export.run_export(
        "testserver",
        "testdb",
        2026,
        str(output_path),
        str(query_file),
        progress_callback=messages.append,
    )

    assert result.entries_written == 1
    assert result.skipped_publication_ids == [bad_row["publication_id"]]
    content = output_path.read_text(encoding="utf-8")
    assert "TY  - CHAP" in content
    assert content.startswith("% Exported")
    assert any("Wrote 1 entries" in m for m in messages)


def test_run_export_header_uses_percent_comment_lines(monkeypatch, tmp_path):
    """RIS has no comment syntax -- '%' lines must not match the
    'TAG  - value' pattern, so a real RIS reader silently drops them
    instead of choking on them or treating them as part of an entry."""
    query_file = tmp_path / "query.sql"
    query_file.write_text("SELECT 1")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [_book_section_row()]
    mock_conn.cursor.return_value = mock_cursor
    mock_pyodbc = MagicMock()
    mock_pyodbc.connect.return_value = mock_conn
    monkeypatch.setattr(ris_export, "pyodbc", mock_pyodbc)

    output_path = tmp_path / "out.ris"
    ris_export.run_export("s", "d", 2026, str(output_path), str(query_file))

    for line in output_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("%") or not line.strip():
            continue
        # first non-comment, non-blank line must be a real entry
        assert line.startswith("TY  - ")
        break
