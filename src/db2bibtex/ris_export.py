"""
ris_export.py

Core export logic for db2ris: connects to the same SQL Server publications
database and query.sql as db2bibtex.exporter, but builds a RIS (.ris) file
instead of BibTeX.

Why this exists alongside db2bibtex.exporter: BibTeX has no @newspaper or
@magazine entry type -- @article is the only periodical entry type the
format defines -- so db2bibtex.exporter.ENTRY_TYPE_MAP has to map Journal,
Magazine, and Newspaper articles all to the same BibTeX "article" type, and
Zotero's BibTeX-import translator then turns every one of those into item
type "Journal Article" on import, with no way to tell them apart afterward
(confirmed against Zotero's actual RIS.js/BibTeX.js translator source, not
just the format specs). RIS defines distinct type codes for all three
(JOUR/MGZN/NEWS), confirmed against Zotero's real RIS import type map, so
this exporter recovers that distinction db2bibtex.zotero_item_type_fix
otherwise has to repair after the fact.

This module has no CLI/GUI dependencies -- ris_cli.py imports from here,
mirroring exporter.py/cli.py's split. See db2bibtex.db for everything
here that isn't RIS-specific (SQL Server connection/config, query.sql
loading, author splitting, PDF/URL resolution, the "Source DB:
publication_id ..." note text).

RIS field/tag reference used while building RIS_TYPE_MAP and the field
mapping below: Zotero's own RIS.js translator (exportTypeMap/importTypeMap
and fieldMap), not just the bare RIS spec -- e.g. RIS has no continuation
convention beyond "next line doesn't match a tag, so it's a continuation
of the previous value" (confirmed in RISReader._getTagValue), and Zotero
resolves ISBN vs ISSN, place vs university vs institution, etc. from the
*item type*, not from a different RIS tag -- so this exporter always
writes the same tag (SN, CY, PB) regardless of type and lets Zotero's
importer do that resolution, exactly as it would for a RIS file from any
other source.

Notes on design decisions:
    - reference_type/pub_type is mapped to a RIS type code, not a BibTeX
      entry type. Add or edit RIS_TYPE_MAP below if reference_type values
      differ from what's guessed here (run `SELECT DISTINCT reference_type
      FROM publication` against your own database, same as the note in
      db2bibtex.exporter.ENTRY_TYPE_MAP).
    - No citation-key concept: RIS has no BibTeX-style key field (its `ID`
      tag is EndNote-specific and explicitly ignored on import by Zotero's
      translator), so there's nothing here corresponding to
      db2bibtex.exporter.make_cite_key.
    - Pages are split into SP (start) and EP (end) the same way Zotero's
      own RIS export does: split on the first hyphen-like character; if
      there isn't one, the whole value goes to SP.
    - PY is written as the bare year (matches how the DB stores pub_year;
      no date parsing needed, unlike Zotero's own translator which has to
      parse whatever free-text date its own items carry).
    - T2 (secondary title) is only written for the item types where
      Zotero's own field map actually uses it for something meaningful
      (publicationTitle/bookTitle/conferenceName) -- writing it for other
      types would just get silently ignored or dumped somewhere unhelpful.
    - Editors go to A2 for bookSection/conferencePaper, A3 for book --
      matches Zotero's own A2/A3 editor mapping per item type exactly;
      getting this wrong means the editor either doesn't show up at all
      or shows up as a second set of authors.
    - No BibTeX-style character escaping: RIS is plain text, not LaTeX.
      Values are only stripped, matching Zotero's own RIS writer (which
      also skips empty values entirely rather than emitting a blank tag).
    - Records with no usable title or author are skipped and reported
      (same rule as db2bibtex.exporter.run_export).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from db2bibtex.db import (
    DEFAULT_DRIVER,
    PyodbcMissingError,
    QueryFileMissingError,
    build_conn_str,
    build_source_note,
    get_field as _get,
    load_config,
    load_query,
    match_reference_type,
    resolve_pdf_and_url,
    save_config,
    split_authors_list,
)

try:
    import pyodbc
except ImportError:
    pyodbc = None


# ---------------------------------------------------------------------------
# Reference type -> RIS type code. EDIT to match the actual reference_type
# values in your database (run a SELECT DISTINCT against whatever column
# your query.sql aliases to reference_type/pub_type) and adjust keys below
# (lowercased, matched by substring). Verified against Zotero's real RIS
# import type map (RIS.js exportTypeMap, which doubles as the import map):
# JOUR/MGZN/NEWS import as distinct Zotero item types (journalArticle/
# magazineArticle/newspaperArticle) -- this is the whole reason db2ris
# exists. ADVS/COMP/MAP recover Zotero item types (film/computerProgram/
# map) that db2bibtex.exporter's BibTeX map has no equivalent for and so
# falls back to "misc" for.
# ---------------------------------------------------------------------------
RIS_TYPE_MAP = {
    "journal article": "JOUR",
    "magazine article": "MGZN",
    "newspaper article": "NEWS",
    "book section": "CHAP",
    "book chapter": "CHAP",
    "edited book": "BOOK",
    "book": "BOOK",
    "conference paper": "CONF",
    "conference proceedings": "CONF",
    "thesis": "THES",
    "dissertation": "THES",
    "report": "RPRT",
    "government document": "RPRT",
    "unpublished": "UNPB",
    "manuscript": "MANSCPT",
    "web page": "ELEC",
    "web document": "ELEC",
    "electronic article": "JOUR",
    "audiovisual material": "ADVS",
    "computer program": "COMP",
    "map": "MAP",
}
# Zotero's own DEFAULT_EXPORT_TYPE/DEFAULT_IMPORT_TYPE -- "GEN" imports as
# journalArticle, so an unrecognized reference_type behaves the same way an
# unrecognized RIS file from any other source would.
DEFAULT_RIS_TYPE = "GEN"

# Item types where Zotero's own RIS field map uses T2 for something
# meaningful (publicationTitle for periodicals, bookTitle for a book
# section, conferenceName for a conference paper). Writing T2 for any
# other type would map to nothing useful on import.
RIS_T2_TYPES = {"JOUR", "MGZN", "NEWS", "CHAP", "CONF"}

# RIS line format per Zotero's own writer: "TAG  - value\r\n" (two spaces,
# dash, one space), CRLF line endings, "ER  - " terminates each record
# followed by a blank line before the next one.
_NEWLINE = "\r\n"
# Matches the same hyphen-like characters Zotero's own pages-splitting
# regex does (SP/EP), so "12-34" -> SP=12, EP=34 the same way a page range
# exported directly from Zotero would come out.
_PAGE_RANGE_RE = re.compile(r"(.+?)[-­‐-―−⸺⸻\s]+(.+)")


def guess_ris_type(reference_type: Optional[str], pub_type: Optional[str] = None) -> str:
    """Map reference_type/pub_type to a RIS type code.

    Args:
        reference_type: The publication's reference_type column.
        pub_type: The publication's pub_type column (fallback, same order
            db2bibtex.exporter.guess_entry_type uses).

    Returns:
        A RIS type code, e.g. "JOUR", "NEWS", "CHAP".
    """
    return match_reference_type(reference_type, pub_type, RIS_TYPE_MAP) or DEFAULT_RIS_TYPE


def _split_pages(pages: str) -> "tuple[str, Optional[str]]":
    """Split a pages string into (start, end), matching Zotero's own RIS
    export logic exactly. Returns (pages, None) if there's no separator."""
    m = _PAGE_RANGE_RE.match(pages.strip())
    if m:
        return m.group(1), m.group(2)
    return pages, None


def build_ris_entry(row: Any) -> str:
    """Build one RIS entry from a database row.

    Args:
        row: A pyodbc.Row (or dict with matching keys) from the query in
            query.sql (the same query db2bibtex.exporter uses -- see
            query.sql.example).

    Returns:
        A formatted "TY  - ...\\r\\n...\\r\\nER  - \\r\\n" RIS entry string.
    """
    entry_type = guess_ris_type(_get(row, "reference_type"), _get(row, "pub_type"))

    lines = [f"TY  - {entry_type}"]

    def add(tag: str, value: Any) -> None:
        if value is None:
            return
        v = str(value).strip()
        if not v:
            return
        lines.append(f"{tag}  - {v}")

    for author in split_authors_list(_get(row, "author")):
        add("AU", author)

    add("TI", _get(row, "title"))
    add("PY", _get(row, "pub_year"))

    secondary_title = _get(row, "secondary_title")
    if secondary_title and entry_type in RIS_T2_TYPES:
        add("T2", secondary_title)

    secondary_author = _get(row, "secondary_author")
    if secondary_author:
        editor_tag = "A3" if entry_type == "BOOK" else "A2" if entry_type in ("CHAP", "CONF") else None
        if editor_tag:
            for editor in split_authors_list(secondary_author):
                add(editor_tag, editor)

    add("VL", _get(row, "volume"))
    add("IS", _get(row, "issue"))

    pages = _get(row, "pages")
    if pages and str(pages).strip():
        start, end = _split_pages(str(pages))
        add("SP", start)
        add("EP", end)

    add("PB", _get(row, "publisher"))
    add("CY", _get(row, "place_published"))

    if entry_type == "THES":
        add("M3", _get(row, "type_of_work"))

    add("SN", _get(row, "isbn_issn"))
    add("DO", _get(row, "doi"))
    add("AB", _get(row, "abstract"))

    pub_number = _get(row, "pub_number")
    pdf_link, url = resolve_pdf_and_url(
        pub_number,
        _get(row, "online_linkage"),
        _get(row, "online_pdf"),
        _get(row, "pdf"),
    )
    if pdf_link:
        add("L1", pdf_link)
    if url:
        add("UR", url)

    # Note text is shared with exporter.py's `note` field -- both land in
    # a Zotero child note, and db2bibtex.zotero_item_type_fix relies on
    # this exact text to match a Zotero item back to its source row
    # regardless of which exporter produced it.
    add(
        "N1",
        build_source_note(
            _get(row, "notes"), _get(row, "publication_id"), pub_number, _get(row, "catalog_id")
        ),
    )

    # KW -> imports as a Zotero tag, same pub_number:NNNN convention as
    # exporter.py's `keywords` field.
    if pub_number:
        add("KW", f"pub_number:{pub_number}")

    lines.append("ER  - ")
    return _NEWLINE.join(lines) + _NEWLINE


def fetch_rows(
    server: str,
    database: str,
    before_year: int,
    query_file: "str | Path",
    *,
    driver: str = DEFAULT_DRIVER,
    trusted_connection: bool = False,
    uid: Optional[str] = None,
    pwd: Optional[str] = None,
    trust_server_certificate: bool = True,
    encrypt: bool = True,
) -> list:
    """Connect to SQL Server and fetch rows matching the prospective filter.

    Identical to db2bibtex.exporter.fetch_rows -- both read the same
    query.sql -- kept as a separate function (rather than importing
    exporter.fetch_rows directly) only so each module's own `pyodbc` name
    stays independently mockable in tests.

    Raises:
        PyodbcMissingError: If pyodbc could not be imported.
        QueryFileMissingError: If query_file doesn't exist.
    """
    if pyodbc is None:
        raise PyodbcMissingError(
            "pyodbc is required for database export but is not importable "
            "(install it and/or the unixODBC system library)"
        )

    query = load_query(query_file)
    conn_str = build_conn_str(
        driver, server, database, trusted_connection, uid, pwd, encrypt, trust_server_certificate
    )

    conn = pyodbc.connect(conn_str)
    try:
        cursor = conn.cursor()
        cursor.execute(query, before_year)
        return cursor.fetchall()
    finally:
        conn.close()


@dataclass
class ExportResult:
    """Outcome of an export run."""

    output_path: str
    entries_written: int = 0
    skipped_publication_ids: list = field(default_factory=list)


def run_export(
    server: str,
    database: str,
    before_year: int,
    output_path: "str | Path",
    query_file: "str | Path",
    *,
    driver: str = DEFAULT_DRIVER,
    trusted_connection: bool = False,
    uid: Optional[str] = None,
    pwd: Optional[str] = None,
    trust_server_certificate: bool = True,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> ExportResult:
    """Run the full export: query the database and write a .ris file.

    Args mirror db2bibtex.exporter.run_export exactly (same query.sql,
    same --before-year filter semantics).

    Returns:
        An ExportResult describing what was written.
    """

    def report(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    report(f"Connecting to {server}/{database}...")
    rows = fetch_rows(
        server,
        database,
        before_year,
        driver=driver,
        trusted_connection=trusted_connection,
        uid=uid,
        pwd=pwd,
        trust_server_certificate=trust_server_certificate,
        query_file=query_file,
    )
    report(f"Fetched {len(rows)} candidate records.")

    skipped: list = []
    entries: list = []

    for row in rows:
        if not _get(row, "title") or not _get(row, "author"):
            skipped.append(_get(row, "publication_id"))
            continue
        entries.append(build_ris_entry(row))

    # RIS has no comment syntax, but any line that doesn't match the
    # "TAG  - value" pattern and isn't a continuation of a tag already in
    # progress is silently dropped by Zotero's RIS reader ("outside of RIS
    # record") rather than erroring -- confirmed in RISReader._getTagValue.
    # These plain-text lines are exactly that: informational only, safe to
    # leave in, and never parsed as part of the first real entry (as long
    # as none of them happen to start with two letters/digits + "  - ",
    # which a leading "%" avoids on purpose, mirroring exporter.py's BibTeX
    # comment header).
    header = (
        f"% Exported {datetime.now().isoformat(timespec='seconds')} "
        f"from {server}/{database}{_NEWLINE}"
        f"% Query: {query_file} (before_year < {before_year}; additional "
        f"filtering, if any, is defined in that file){_NEWLINE}"
        f"% {len(entries)} records exported, {len(skipped)} skipped "
        f"(missing title or author){_NEWLINE}{_NEWLINE}"
    )

    output_path = str(output_path)
    with open(output_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(header)
        fh.write(_NEWLINE.join(entries))
        if entries:
            fh.write(_NEWLINE)

    report(f"Wrote {len(entries)} entries to {output_path}")
    if skipped:
        report(f"Skipped {len(skipped)} records (missing title/author): {skipped}")

    return ExportResult(
        output_path=output_path,
        entries_written=len(entries),
        skipped_publication_ids=skipped,
    )
