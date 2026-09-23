"""
exporter.py

Core export logic for db2bibtex: connects to a SQL Server publications
database and builds a BibTeX (.bib) file of Published H.J. Andrews
Experimental Forest / LTER publications, suitable for import into Zotero.

This module has no CLI/GUI dependencies -- cli.py and gui.py both import
from here so behavior stays in sync between the two front ends.

The actual SQL query (table/column names, join structure, and filter
values) is kept out of this module entirely -- it lives in an external,
gitignored query.sql file so the internal database schema isn't published
alongside this source. See query.sql.example for the expected shape and
load_query() below for how it's read in. The query is expected to select a
row per candidate publication, filtered down to published, non-abstract
records in scope for this tool, with one `?` parameter bound to
--before-year.

Notes on design decisions:
    - reference_type/pub_type is mapped to a BibTeX entry type. Add or edit
      ENTRY_TYPE_MAP below if reference_type values differ from what's
      guessed here.
    - PDF link resolution: online_pdf (a stored URL) is used verbatim when
      present; otherwise, if the pdf flag column is 'T'/true, a URL is
      derived from pub_number using the same convention the Andrews
      Forest Drupal publications page uses
      (https://andrewsforest.oregonstate.edu/pubs/pdf/pub<pub_number>.pdf)
      -- that flag is frequently set even when online_pdf itself is empty.
      The result is written to the `pdf` field (Zotero's built-in BibTeX
      import translator auto-attaches this as a downloadable PDF link
      whenever the value contains "://").
    - `url` is deliberately NOT the same as `pdf`: whenever a PDF link was
      resolved, url points at the publications detail page instead
      (https://andrewsforest.oregonstate.edu/publications/<pub_number>),
      so the item's clickable URL in Zotero opens the catalog record, not
      a raw PDF download. online_linkage is used for url only when no PDF
      link could be resolved at all.
    - author / secondary_author are delimited with '//' in the source data.
      ';' and newline are kept as fallback delimiters.
    - Citation keys use the production convention: 'AND' + pub_number
      (e.g. AND2135). Falls back to an Author+Year+Title heuristic only if
      pub_number is missing.
    - pub_number is also written to a `keywords` field, which Zotero's
      BibTeX import maps to a tag (e.g. pub_number:2135).
    - All free-text fields are escaped for BibTeX special characters.
    - Records with no usable title or author are skipped and reported.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from db2bibtex.db import (
    ANDREWS_FOREST_BASE_URL,
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
# Reference type -> BibTeX entry type. EDIT to match the actual
# reference_type values in your database (run a SELECT DISTINCT against
# whatever column your query.sql aliases to reference_type/pub_type) and
# adjust keys below (lowercased, matched by substring).
# ---------------------------------------------------------------------------
ENTRY_TYPE_MAP = {
    "journal article": "article",
    "magazine article": "article",
    "newspaper article": "article",
    "book section": "incollection",
    "book chapter": "incollection",
    "edited book": "book",
    "book": "book",
    "conference paper": "inproceedings",
    "conference proceedings": "inproceedings",
    "thesis": "phdthesis",  # refined below using type_of_work
    "dissertation": "phdthesis",
    "report": "techreport",
    "government document": "techreport",
    "unpublished work": "unpublished",
    "manuscript": "unpublished",
    "web page": "misc",
    "electronic article": "misc",
}
DEFAULT_ENTRY_TYPE = "misc"

BIBTEX_SPECIAL_CHARS = {
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

# The real query (real table/column names, real project-ID filters) lives in
# an external, gitignored query.sql file -- never in this module -- so the
# internal database schema isn't published alongside the public source. See
# query.sql.example for the expected shape and db2bibtex.db.load_query() for
# how it's read in (at call time, not import time, since the file won't exist
# in a fresh clone or in CI). There's deliberately no default path or
# database name baked in here -- both must be supplied explicitly (CLI
# flags / GUI fields), so the public codebase never assumes any particular
# institution's file layout or database naming. db2ris's ris_export.py reads
# this exact same query.sql -- both formats need the same source columns.


def escape_bibtex(value: Any) -> str:
    """Escape BibTeX special characters in free text.

    Args:
        value: Raw field value (any type; coerced to str). None becomes "".

    Returns:
        The value with BibTeX special characters escaped. Best-effort pass,
        not a full LaTeX parser -- existing LaTeX commands are left alone.
    """
    if value is None:
        return ""
    value = str(value).strip()
    return "".join(BIBTEX_SPECIAL_CHARS.get(ch, ch) for ch in value)


def split_authors(raw: Optional[str]) -> str:
    """Convert the DB's multi-author field into BibTeX ' and '-joined form.

    Args:
        raw: Raw author string, e.g. "Cooper, G. M.//Lattin, John D.".
            The publication table delimits multiple authors with '//'.
            ';' and newline are kept as fallback delimiters (see
            db2bibtex.db.split_authors_list for the actual parsing --
            db2ris's ris_export.py uses the same list, one AU line per
            author, instead of joining it into one string).

    Returns:
        Authors joined with ' and ', or "" if raw is empty.
    """
    return " and ".join(split_authors_list(raw))


def guess_entry_type(
    reference_type: Optional[str],
    pub_type: Optional[str],
    type_of_work: Optional[str],
) -> str:
    """Map reference_type/pub_type to a BibTeX entry type.

    Args:
        reference_type: The publication's reference_type column.
        pub_type: The publication's pub_type column (fallback).
        type_of_work: The type_of_work column, used to distinguish
            mastersthesis from phdthesis.

    Returns:
        A BibTeX entry type string, e.g. "article", "incollection".
    """
    bib_type = match_reference_type(reference_type, pub_type, ENTRY_TYPE_MAP) or DEFAULT_ENTRY_TYPE

    if bib_type == "phdthesis" and type_of_work:
        if "master" in type_of_work.strip().lower():
            return "mastersthesis"
    return bib_type


def make_cite_key(
    author: Optional[str],
    pub_year: Any,
    title: Optional[str],
    used_keys: set,
    pub_number: Optional[Any] = None,
) -> str:
    """Generate a BibTeX citation key.

    Matches the production Andrews Forest convention of 'AND' + pub_number
    (e.g. AND2135). Falls back to an Author+Year+TitleWord heuristic only
    if pub_number is missing.

    Args:
        author: Raw author string (used only for the fallback heuristic).
        pub_year: Publication year (used only for the fallback heuristic).
        title: Publication title (used only for the fallback heuristic).
        used_keys: Set of citation keys already assigned in this run;
            mutated in place to record the returned key.
        pub_number: The publication's pub_number, if available.

    Returns:
        A unique citation key, suffixed with "_2", "_3", ... on collision.
    """
    if pub_number:
        key = f"AND{pub_number}"
    else:
        first_author = "Unknown"
        if author:
            first_part = re.split(r";", author)[0]
            last_name = re.split(r",", first_part)[0].strip()
            first_author = re.sub(r"[^A-Za-z]", "", last_name) or "Unknown"
        year = str(pub_year) if pub_year else "n.d."
        title_word = ""
        if title:
            words = re.findall(r"[A-Za-z]+", title)
            if words:
                title_word = words[0].capitalize()
        key = f"{first_author}{year}{title_word}"

    base_key = key
    i = 2
    while key in used_keys:
        key = f"{base_key}_{i}"
        i += 1
    used_keys.add(key)
    return key


def build_bibtex_entry(row: Any, used_keys: set) -> str:
    """Build one BibTeX entry from a database row.

    Args:
        row: A pyodbc.Row (or dict with matching keys) from the query in
            query.sql (aliased to the canonical column names this function
            expects -- see query.sql.example).
        used_keys: Set of citation keys already assigned in this run;
            mutated in place.

    Returns:
        A formatted "@type{key, field = {value}, ...}" BibTeX entry string.
    """
    entry_type = guess_entry_type(
        _get(row, "reference_type"), _get(row, "pub_type"), _get(row, "type_of_work")
    )
    author_field = split_authors(_get(row, "author"))
    cite_key = make_cite_key(
        _get(row, "author"),
        _get(row, "pub_year"),
        _get(row, "title"),
        used_keys,
        pub_number=_get(row, "pub_number"),
    )

    fields: dict[str, str] = {}
    fields["author"] = author_field
    fields["title"] = escape_bibtex(_get(row, "title"))
    pub_year = _get(row, "pub_year")
    fields["year"] = str(pub_year) if pub_year else ""

    secondary_title = _get(row, "secondary_title")
    if secondary_title:
        if entry_type == "article":
            fields["journal"] = escape_bibtex(secondary_title)
        elif entry_type in ("incollection", "inproceedings"):
            fields["booktitle"] = escape_bibtex(secondary_title)
        else:
            fields["series"] = escape_bibtex(secondary_title)

    secondary_author = _get(row, "secondary_author")
    if secondary_author and entry_type in ("incollection", "book"):
        fields["editor"] = split_authors(secondary_author)

    volume = _get(row, "volume")
    if volume:
        fields["volume"] = escape_bibtex(volume)
    issue = _get(row, "issue")
    if issue:
        fields["number"] = escape_bibtex(issue)
    pages = _get(row, "pages")
    if pages:
        fields["pages"] = escape_bibtex(pages)

    publisher = _get(row, "publisher")
    if publisher:
        if entry_type == "techreport":
            fields["institution"] = escape_bibtex(publisher)
        elif entry_type in ("phdthesis", "mastersthesis"):
            fields["school"] = escape_bibtex(publisher)
        else:
            fields["publisher"] = escape_bibtex(publisher)

    place_published = _get(row, "place_published")
    if place_published:
        fields["address"] = escape_bibtex(place_published)
    edition = _get(row, "edition")
    if edition:
        fields["edition"] = escape_bibtex(edition)
    doi = _get(row, "doi")
    if doi:
        fields["doi"] = escape_bibtex(doi)

    isbn_issn = _get(row, "isbn_issn")
    if isbn_issn:
        isbn_issn = isbn_issn.strip()
        digits = re.sub(r"[^0-9Xx]", "", isbn_issn)
        fields["issn" if len(digits) <= 8 else "isbn"] = escape_bibtex(isbn_issn)

    abstract = _get(row, "abstract")
    if abstract:
        fields["abstract"] = escape_bibtex(abstract)

    # PDF/URL resolution rules are shared with ris_export.py -- see
    # db2bibtex.db.resolve_pdf_and_url's docstring. `pdf` is what Zotero's
    # BibTeX import translator auto-attaches (any value containing "://").
    pub_number = _get(row, "pub_number")
    pdf_link, url = resolve_pdf_and_url(
        pub_number,
        _get(row, "online_linkage"),
        _get(row, "online_pdf"),
        _get(row, "pdf"),
    )
    if pdf_link:
        fields["pdf"] = pdf_link
    if url:
        fields["url"] = url

    # Note text is shared with ris_export.py's N1 field -- both land in a
    # Zotero child note, and db2bibtex.zotero_item_type_fix relies on this
    # exact "Source DB: publication_id ..." text to match a Zotero item
    # back to its source row regardless of which exporter produced it.
    fields["note"] = escape_bibtex(
        build_source_note(
            _get(row, "notes"), _get(row, "publication_id"), pub_number, _get(row, "catalog_id")
        )
    )

    # keywords -> imports as a Zotero tag, so pub_number is
    # filterable/searchable via the tag selector.
    if pub_number:
        fields["keywords"] = escape_bibtex(f"pub_number:{pub_number}")

    lines = [f"@{entry_type}{{{cite_key},"]
    field_lines = [f"  {k} = {{{v}}}" for k, v in fields.items() if v]
    lines.append(",\n".join(field_lines))
    lines.append("}\n")
    return "\n".join(lines)


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

    Args:
        server: SQL Server hostname/instance.
        database: SQL Server database name.
        before_year: Export records with pub_year strictly less than this.
        query_file: Path to the SQL query file (see load_query()). Required
            -- there is no default, so callers must always be explicit.
        driver: ODBC driver name.
        trusted_connection: Use Windows trusted auth instead of uid/pwd.
        uid: SQL auth username (ignored if trusted_connection is True).
        pwd: SQL auth password (ignored if trusted_connection is True).
        trust_server_certificate: Set TrustServerCertificate=yes. ODBC
            Driver 18 defaults to Encrypt=yes and refuses to connect
            without a trusted cert chain unless this is set.
        encrypt: Set Encrypt=yes/no.

    Returns:
        A list of pyodbc.Row objects from the query in query_file.

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
    """Run the full export: query the database and write a .bib file.

    Args:
        server: SQL Server hostname/instance.
        database: SQL Server database name.
        before_year: Export records with pub_year strictly less than this.
        output_path: Destination .bib file path.
        query_file: Path to the SQL query file (see load_query()). Required
            -- there is no default, so callers must always be explicit.
        driver: ODBC driver name.
        trusted_connection: Use Windows trusted auth instead of uid/pwd.
        uid: SQL auth username.
        pwd: SQL auth password.
        trust_server_certificate: Set TrustServerCertificate=yes.
        progress_callback: Optional callable invoked with human-readable
            status strings as the export proceeds (e.g. for a GUI log pane).

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

    used_keys: set = set()
    skipped: list = []
    entries: list = []

    for row in rows:
        if not _get(row, "title") or not _get(row, "author"):
            skipped.append(_get(row, "publication_id"))
            continue
        entries.append(build_bibtex_entry(row, used_keys))

    header = (
        f"% Exported {datetime.now().isoformat(timespec='seconds')} "
        f"from {server}/{database}\n"
        f"% Query: {query_file} (before_year < {before_year}; additional "
        f"filtering, if any, is defined in that file)\n"
        f"% {len(entries)} records exported, {len(skipped)} skipped "
        f"(missing title or author)\n\n"
    )

    output_path = str(output_path)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(header)
        fh.write("\n\n".join(entries))
        fh.write("\n")

    report(f"Wrote {len(entries)} entries to {output_path}")
    if skipped:
        report(f"Skipped {len(skipped)} records (missing title/author): {skipped}")

    return ExportResult(
        output_path=output_path,
        entries_written=len(entries),
        skipped_publication_ids=skipped,
    )
