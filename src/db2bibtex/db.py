"""
db.py

Format-agnostic core shared by db2bibtex's BibTeX exporter (exporter.py)
and its RIS exporter (ris_export.py): SQL Server connection-string
assembly, the external query-file convention, db_config.ini's
[Database]/[Driver] sections, author-list splitting, PDF/URL resolution,
and the "Source DB: publication_id ..." note text that db2bibtex.compare
and db2bibtex.zotero_item_type_fix both rely on to match a Zotero item
back to its source row -- regardless of which format was used to import
it (BibTeX's `note` field and RIS's `N1` field both land in the same kind
of Zotero child note, so this text only ever needs to be built once, here).

Nothing in this module knows about BibTeX or RIS field/tag names or
escaping rules -- it only knows about the source database's columns and
this project's conventions for interpreting them. Both format-specific
exporters, and db2bibtex.zotero_item_type_fix's own SQL Server lookup,
import from here rather than duplicating this logic.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Optional


class PyodbcMissingError(RuntimeError):
    """Raised when a database operation is attempted but pyodbc is unavailable."""


class QueryFileMissingError(FileNotFoundError):
    """Raised when the SQL query file cannot be found.

    Subclasses FileNotFoundError so existing ``except FileNotFoundError``
    handlers still catch it, while carrying a message actionable enough to
    show directly to a user (CLI stderr or a GUI message box) instead of a
    bare traceback.
    """


DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"

ANDREWS_FOREST_BASE_URL = "https://andrewsforest.oregonstate.edu"


def load_query(query_file: "str | Path") -> str:
    """Load the SQL query text from an external file.

    Args:
        query_file: Path to the SQL file (e.g. "query.sql", after copying
            and adapting query.sql.example). db2bibtex and db2ris share the
            same query.sql -- both need the same source columns, just
            formatted differently on the way out.

    Returns:
        The file's contents as a string.

    Raises:
        QueryFileMissingError: If the file doesn't exist, with guidance on
            how to create it.
    """
    path = Path(query_file)
    if not path.exists():
        raise QueryFileMissingError(
            f"Query file not found: {path}\n"
            "Copy query.sql.example to query.sql (or pass --query-file / use "
            "the Query File field) and adapt it to your database's table and "
            "column names before running db2bibtex."
        )
    return path.read_text(encoding="utf-8")


def build_conn_str(
    driver: str,
    server: str,
    database: str,
    trusted_connection: bool = False,
    uid: Optional[str] = None,
    pwd: Optional[str] = None,
    encrypt: bool = True,
    trust_server_certificate: bool = True,
) -> str:
    """Assemble a pyodbc connection string.

    Pure string assembly, no pyodbc dependency -- callers do their own
    ``if pyodbc is None`` check (kept local to each caller's module so
    tests can monkeypatch that module's own ``pyodbc`` name, same
    convention this project already used before this was factored out).
    """
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}"]
    if trusted_connection:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={uid}")
        parts.append(f"PWD={pwd}")
    parts.append(f"Encrypt={'yes' if encrypt else 'no'}")
    parts.append(f"TrustServerCertificate={'yes' if trust_server_certificate else 'no'}")
    return ";".join(parts) + ";"


def load_config(config_path: "str | Path") -> dict:
    """Read connection settings from an .ini file.

    Args:
        config_path: Path to a db_config.ini-style file with [Database]
            and [Driver] sections (see db_config.ini.example).

    Returns:
        A dict with keys: server, database, uid, pwd, driver,
        trust_server_certificate.
    """
    cp = configparser.ConfigParser()
    read_files = cp.read(config_path)
    if not read_files:
        raise FileNotFoundError(f"Could not read config file: {config_path}")

    db = cp["Database"] if cp.has_section("Database") else {}
    drv = cp["Driver"] if cp.has_section("Driver") else {}

    driver = drv.get("driver", DEFAULT_DRIVER).strip("{}")

    return {
        "server": db.get("server"),
        "database": db.get("database"),
        "uid": db.get("username"),
        "pwd": db.get("password"),
        "driver": driver,
        "trust_server_certificate": cp.getboolean(
            "Database", "trustservercertificate", fallback=True
        )
        if cp.has_section("Database")
        else True,
    }


def save_config(
    config_path: "str | Path",
    server: str,
    database: str,
    driver: str = DEFAULT_DRIVER,
    uid: Optional[str] = None,
    pwd: Optional[str] = None,
    trust_server_certificate: bool = True,
) -> None:
    """Write connection settings to an .ini file in db_config.ini format.

    Args:
        config_path: Destination path.
        server: SQL Server hostname/instance.
        database: Database name.
        driver: ODBC driver name.
        uid: SQL auth username, if any.
        pwd: SQL auth password, if any. Written in plaintext -- callers
            (e.g. the GUI) should warn the user before invoking this with
            a real password.
        trust_server_certificate: Written as "yes"/"no".
    """
    cp = configparser.ConfigParser()
    cp.read(config_path)  # preserve any other sections already in the file
    # (e.g. [Zotero]/[CrossRef], written by zotero_author_complete.save_config
    # -- multiple features can share one db_config.ini)
    cp["Database"] = {
        "server": server or "",
        "database": database or "",
        "username": uid or "",
        "password": pwd or "",
        "trustservercertificate": "yes" if trust_server_certificate else "no",
    }
    cp["Driver"] = {"driver": f"{{{driver}}}" if driver else f"{{{DEFAULT_DRIVER}}}"}
    with open(config_path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def get_field(row, name: str):
    """Fetch a field from a pyodbc.Row or a plain mapping (for tests).

    Both exporter.py's build_bibtex_entry() and ris_export.py's
    build_ris_entry() read the same query.sql row shape this way.
    """
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name)


def split_authors_list(raw: Optional[str]) -> list:
    """Split the DB's multi-author field into individual author strings.

    Args:
        raw: Raw author string, e.g. "Cooper, G. M.//Lattin, John D.".
            The publication table delimits multiple authors with '//'.
            ';' and newline are kept as fallback delimiters.

    Returns:
        A list of stripped author strings (each already "Last, First"),
        or [] if raw is empty. BibTeX joins these with ' and '; RIS writes
        one AU line per entry -- each format's own module does that join.
    """
    if not raw:
        return []
    return [p.strip() for p in re.split(r"\s*//\s*|;\s*|\n", raw) if p.strip()]


def match_reference_type(
    reference_type: Optional[str], pub_type: Optional[str], type_map: dict
) -> Optional[str]:
    """Case-insensitive, word-boundary match of reference_type/pub_type
    against an ordered {phrase: mapped_value} map, first hit wins (dict
    insertion order) -- same algorithm db2bibtex.exporter.guess_entry_type
    and db2bibtex.ris_export.guess_ris_type each apply to their own
    format-specific type map.

    Word-boundary (not bare substring) matching: a short key like "map"
    must not fire on "Unmapped" or any other word that merely contains it
    -- found via a real test case, not hypothetical. Every key already in
    use (single words and multi-word phrases like "conference
    proceedings") still matches exactly the same real reference_type
    values it always did, since those phrases are never embedded mid-word
    in real data.

    Args:
        reference_type: The publication's reference_type column.
        pub_type: The publication's pub_type column (fallback).
        type_map: Ordered dict of lowercased phrase -> mapped value.

    Returns:
        The mapped value from the first matching key, or None if nothing
        matched (callers apply their own default).
    """
    rt = (reference_type or pub_type or "").strip().lower()
    for key, mapped in type_map.items():
        if re.search(r"\b" + re.escape(key) + r"\b", rt):
            return mapped
    return None


def resolve_pdf_and_url(
    pub_number,
    online_linkage: Optional[str],
    online_pdf: Optional[str],
    pdf_flag,
    base_url: str = ANDREWS_FOREST_BASE_URL,
) -> "tuple[Optional[str], Optional[str]]":
    """Resolve a record's PDF link and clickable URL, in priority order:

        1. online_pdf, verbatim, if the source column has a stored URL
        2. else, if the pdf flag column is set, the URL derived from
           pub_number using the same convention the Andrews Forest Drupal
           publications page uses (dbo.publication.pdf is a 'T'/'F' flag
           there, not a URL -- the page builds
           ".../pubs/pdf/pub<pub_number>.pdf" itself when the flag is
           true; online_pdf frequently isn't populated even though a PDF
           exists at that address)

    `url` is deliberately NOT the same as the PDF link: whenever a PDF
    link was resolved, url points at the publications detail page instead
    (.../publications/<pub_number>), so the item's clickable URL opens the
    catalog record rather than a raw PDF download. online_linkage is used
    for url only when no PDF link could be resolved at all.

    Returns:
        (pdf_link, url) -- either may be None.
    """
    pdf_link = None
    if online_pdf:
        pdf_link = online_pdf.strip()
    elif pub_number and str(pdf_flag).strip().upper() in ("T", "TRUE", "1"):
        pdf_link = f"{base_url}/pubs/pdf/pub{pub_number}.pdf"

    url = None
    if pdf_link:
        if pub_number:
            url = f"{base_url}/publications/{pub_number}"
        elif online_linkage:
            url = online_linkage.strip()
    elif online_linkage:
        url = online_linkage.strip()

    return pdf_link, url


def build_source_note(notes: Optional[str], publication_id, pub_number, catalog_id) -> str:
    """Build the "Source DB: publication_id X; pub_number Y; catalog_id Z"
    text written into every exported record (BibTeX's `note` field, RIS's
    `N1` tag) -- both land in a standalone Zotero child note on import, and
    db2bibtex.zotero_item_type_fix.find_pub_ids_by_parent_note() parses
    this exact text back out to match a Zotero item to its source row.
    Kept identical across formats on purpose: that matching logic must not
    care which exporter produced the note.

    Returns:
        The raw (unescaped) note text -- exporter.py BibTeX-escapes it
        before writing; ris_export.py writes it as-is (RIS is plain text).
    """
    note_parts = []
    if notes:
        note_parts.append(f"Notes: {notes.strip()}")
    note_parts.append(
        f"Source DB: publication_id {publication_id}; "
        f"pub_number {pub_number}; catalog_id {catalog_id}"
    )
    return "; ".join(note_parts)
