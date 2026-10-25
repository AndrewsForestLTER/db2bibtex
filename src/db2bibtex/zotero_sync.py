"""
zotero_sync.py

Syncs the Zotero library directly against the source database through the
Zotero Web API, replacing the manual export -> wipe -> BibTeX import ->
compare -> merge -> fix-item-types -> fix-authors cycle with one dry-run /
live pair of runs.

What one run does:

1. Runs the same query.sql db2bibtex/db2ris export with (default
   --before-year 2100, i.e. every year) and pulls the whole Zotero library
   (parents, child notes, attachments) in one paginated scan.
2. Links Zotero items to database rows by the publication_id in their
   "Source DB: publication_id ..." child note -- the same join key
   db2bibtex.compare and db2bibtex.zotero_item_type_fix use.
3. Classifies every record:

   - ``create``     -- in the database, not in Zotero: created with the right
                       Zotero item type straight away (no BibTeX @article ->
                       Journal Article problem), plus its Source DB note, its
                       pub_number tag and a linked PDF attachment.
   - ``update``     -- linked, but database-owned fields differ: the database
                       wins. Collections, other tags, notes, attachments and
                       relations are left alone.
   - ``adopt``      -- a Zotero item with no Source DB note whose DOI (or
                       title + year) matches exactly one unlinked database
                       row: updated from that row and given the Source DB
                       note, instead of creating a second copy to merge.
   - ``unchanged``  -- linked and already matches.
   - ``zotero-only``, ``not-in-query``, ``zotero-duplicate``,
     ``merged-duplicate`` -- reported, never touched and never deleted.
                       (``merged-duplicate``: one Zotero item carries the
                       Source DB notes of two records still in the query.)

4. Writes an audit workbook (actions, Zotero-only items, database-internal
   duplicate records, summary). Nothing is written to Zotero unless
   ``dry_run`` is False.

Field ownership: the database overwrites a Zotero field only when the
database actually has a value for it -- an empty database column never
blanks a value someone filled in by hand in Zotero. Creators are the one
exception to "database wins": when the database's author list is truncated
to "et al.", an existing complete Zotero creator list is kept (it was
already repaired), and otherwise the full list is looked up by DOI the same
way db2bibtex.zotero_author_complete does.

Writes go through pyzotero's create_items() in batches of 50 (the API
maximum) for both creates and updates -- unlike update_items(), it returns
the API's per-object successful/unchanged/failed result, so one item's
version conflict is reported in the audit instead of being silently lost.
Every update carries the version seen during the scan, so an item edited
elsewhere in the meantime fails with a conflict rather than being
clobbered.
"""

from __future__ import annotations

import copy
import html
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from pyzotero import zotero
except ImportError:  # pragma: no cover -- exercised via ZoteroDepsMissingError tests
    zotero = None

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font
except ImportError:  # pragma: no cover -- exercised via ZoteroDepsMissingError tests
    Workbook = None
    Font = None

from db2bibtex.compare import extract_pub_id
from db2bibtex.db import (
    DEFAULT_DRIVER,
    build_source_note,
    get_field as _get,
    match_reference_type,
    resolve_pdf_and_url,
    split_authors_list,
)
from db2bibtex import exporter
from db2bibtex import zotero_author_complete as authors_mod

log = logging.getLogger(__name__)

DEFAULT_QUERY_FILE = "query.sql"
# query.sql filters on pub_year < ?; a far-future year means "every year".
DEFAULT_BEFORE_YEAR = 2100
BATCH_SIZE = 50  # Zotero Web API maximum objects per write request
PAGE_SIZE = 100  # Zotero Web API maximum items per read request
PAGE_RETRIES = 5
RETRY_BASE_SEC = 5.0

# reference_type phrase (lowercased, word-boundary match, first hit wins) ->
# Zotero item type. Mirrors db2bibtex.ris_export.RIS_TYPE_MAP, translated to
# the Zotero item types Zotero's RIS importer maps those codes to -- so a
# synced item gets the same type a db2ris import would have given it.
ZOTERO_TYPE_MAP = {
    "journal article": "journalArticle",
    "magazine article": "magazineArticle",
    "newspaper article": "newspaperArticle",
    "book section": "bookSection",
    "book chapter": "bookSection",
    "edited book": "book",
    "book": "book",
    "conference paper": "conferencePaper",
    "conference proceedings": "conferencePaper",
    "thesis": "thesis",
    "dissertation": "thesis",
    "report": "report",
    "government document": "report",
    "unpublished": "manuscript",
    "manuscript": "manuscript",
    "web page": "webpage",
    "web document": "webpage",
    "electronic article": "journalArticle",
    "audiovisual material": "film",
    "computer program": "computerProgram",
    "map": "map",
}
# What BibTeX's @misc imports as -- unrecognized reference types keep the
# type they already had under the BibTeX workflow instead of churning.
DEFAULT_ZOTERO_TYPE = "document"

# Where secondary_title goes, first field the item type's template has.
SECONDARY_TITLE_FIELDS = (
    "publicationTitle",
    "bookTitle",
    "proceedingsTitle",
    "websiteTitle",
    "seriesTitle",
    "series",
)
# Where publisher goes, first field the item type's template has (Zotero
# names the publisher base field per type).
PUBLISHER_FIELDS = ("publisher", "institution", "university", "company", "label", "studio")
EDITOR_TYPES = {"book", "bookSection", "conferencePaper"}

PUB_NUMBER_TAG = "pub_number:{}"


class ZoteroDepsMissingError(RuntimeError):
    """Raised when a Zotero operation is attempted but pyzotero/openpyxl
    are not importable."""


@dataclass
class Config:
    """Settings for one sync run."""

    library_id: str
    library_type: str  # 'group' or 'user'
    api_key: str
    server: str
    database: str
    driver: str = DEFAULT_DRIVER
    uid: Optional[str] = None
    pwd: Optional[str] = None
    trusted_connection: bool = False
    trust_server_certificate: bool = True
    query_file: str = DEFAULT_QUERY_FILE
    before_year: int = DEFAULT_BEFORE_YEAR
    crossref_mailto: Optional[str] = None
    fix_authors: bool = True
    dry_run: bool = True
    audit_path: str = "zotero_sync_audit.xlsx"
    collection_key: Optional[str] = None  # new items are added to this collection
    limit: Optional[int] = None  # cap on items written (creates + updates + adopts)
    crossref_rate_limit_sec: float = authors_mod.DEFAULT_CROSSREF_RATE_LIMIT_SEC


@dataclass
class Action:
    """One planned (or applied) change, one audit row."""

    status: str  # create | update | adopt | unchanged | not-in-query | zotero-duplicate | error
    publication_id: str = ""
    pub_number: str = ""
    item_key: str = ""
    title: str = ""
    old_item_type: str = ""
    new_item_type: str = ""
    changes: list = field(default_factory=list)
    detail: str = ""
    data: Optional[dict] = None  # payload to write (create/update/adopt)
    note_text: Optional[str] = None  # Source DB note to add as a child
    pdf_url: Optional[str] = None  # linked PDF attachment to add as a child


@dataclass
class ZoteroOnly:
    """A Zotero item with no Source DB note that sync leaves alone."""

    item_key: str
    title: str
    year: str
    doi: str
    reason: str


@dataclass
class SyncPlan:
    """Everything one run decided, before (or after) writing."""

    actions: list = field(default_factory=list)
    zotero_only: list = field(default_factory=list)
    db_duplicates: list = field(default_factory=list)  # (match_on, value, [pub_ids], [titles])
    skipped_rows: list = field(default_factory=list)  # publication_ids missing title/author

    def counts(self) -> dict:
        """Number of actions per status, plus the Zotero-only count."""
        out: dict = {}
        for a in self.actions:
            out[a.status] = out.get(a.status, 0) + 1
        if self.zotero_only:
            out["zotero-only"] = len(self.zotero_only)
        return out


def load_config(config_path: "str | Path") -> dict:
    """Read [Zotero]/[CrossRef]/[Database]/[Driver] from db_config.ini.

    Args:
        config_path: Path to a db_config.ini-style file.

    Returns:
        The merged settings dicts of db2bibtex.zotero_item_type_fix and
        db2bibtex.zotero_author_complete (crossref_mailto included).
    """
    from db2bibtex import zotero_item_type_fix

    cfg = zotero_item_type_fix.load_config(config_path)
    cfg["crossref_mailto"] = authors_mod.load_config(config_path).get("crossref_mailto")
    return cfg


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

# LaTeX escapes that turn up literally in database text (Zotero's BibTeX
# import unescaped them on the way in).
_LATEX_ESCAPE_RE = re.compile(r"\\([&%$#_])")
# Fields compared/written verbatim -- no dash or LaTeX clean-up.
VERBATIM_FIELDS = {"url", "DOI"}
# Compared on digits (and X) only, so "978-0-19-938021-3" == "9780199380213".
NUMBER_FIELDS = {"ISBN", "ISSN"}


def clean_text(value: Any) -> str:
    """Database text as Zotero's BibTeX import would have stored it: "---"
    becomes an em dash, "--" an en dash, and stray LaTeX escapes ("\\&")
    are dropped, so the first sync doesn't rewrite hundreds of titles over
    punctuation."""
    text = str(value).strip() if value is not None else ""
    text = text.replace("---", "\u2014").replace("--", "\u2013")
    return _LATEX_ESCAPE_RE.sub(r"\1", text)


def norm_text(value: Any) -> str:
    """Collapse whitespace so cosmetic differences don't count as changes."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def same_value(name: str, current: Any, wanted: Any) -> bool:
    """Whether a Zotero field already holds the database value."""
    if name in NUMBER_FIELDS:
        digits = lambda v: re.sub(r"[^0-9X]", "", str(v or "").upper())  # noqa: E731
        return digits(current) == digits(wanted)
    if name in VERBATIM_FIELDS:
        return norm_text(current) == norm_text(wanted)
    return norm_text(clean_text(current)) == norm_text(clean_text(wanted))


def norm_doi(value: Any) -> str:
    """Lowercase a DOI and strip any resolver prefix ("https://doi.org/")."""
    doi = norm_text(value).lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    return re.sub(r"^doi:\s*", "", doi)


def norm_title(value: Any) -> str:
    """Lowercase alphanumerics only -- the title-matching key."""
    return re.sub(r"[^0-9a-z]+", "", html.unescape(str(value or "")).lower())


def year_of(value: Any) -> str:
    """First four-digit year in a Zotero date / database pub_year, or ""."""
    m = re.search(r"\b(\d{4})\b", str(value or ""))
    return m.group(1) if m else ""


def doi_from_extra(extra: str) -> str:
    """DOI written as a "DOI: ..." line in Extra (types without a DOI field)."""
    m = re.search(r"^DOI:\s*(\S+)", extra or "", re.MULTILINE | re.IGNORECASE)
    return m.group(1) if m else ""


def item_doi(data: dict) -> str:
    """An item's DOI, from its DOI field or its Extra field."""
    return norm_doi(data.get("DOI") or doi_from_extra(data.get("extra", "")))


def set_extra_doi(extra: str, doi: str) -> str:
    """Put doi on Extra's "DOI:" line, keeping every other Extra line."""
    lines = [ln for ln in (extra or "").splitlines() if not re.match(r"^DOI:", ln, re.IGNORECASE)]
    return "\n".join([f"DOI: {doi}"] + lines).strip()


def creator_from_author(author: str, creator_type: str = "author") -> dict:
    """Map one database author string to a Zotero creator.

    "Last, First" becomes a two-field creator; anything without a comma
    (usually an organization) becomes a single-field creator.
    """
    if "," in author:
        last, first = (p.strip() for p in author.split(",", 1))
        return {"creatorType": creator_type, "lastName": last, "firstName": first}
    return {"creatorType": creator_type, "name": author.strip()}


def creators_key(creators: list) -> list:
    """Comparison key for a creator list: (type, "First Last") per creator,
    so "Smith, John" and a single-field "John Smith" compare equal."""
    return [
        (c.get("creatorType", "author"), norm_text(authors_mod._creator_display_name(c)).lower())
        for c in creators
    ]


# ---------------------------------------------------------------------------
# Database row -> Zotero item data
# ---------------------------------------------------------------------------

def guess_zotero_type(reference_type: Optional[str], pub_type: Optional[str]) -> str:
    """Map reference_type/pub_type to a Zotero item type."""
    return match_reference_type(reference_type, pub_type, ZOTERO_TYPE_MAP) or DEFAULT_ZOTERO_TYPE


def db_fields(row: Any, template: dict) -> dict:
    """The database-owned field values for one row, restricted to fields the
    target item type's template actually has.

    Only non-empty database values are returned -- see the module docstring
    on field ownership. ``creators`` is always returned (possibly empty)
    and is handled separately by the caller.

    Args:
        row: A query.sql row (pyodbc.Row or dict).
        template: zot.item_template(item_type) for the row's target type.

    Returns:
        {zotero_field: value}.
    """
    item_type = template["itemType"]
    out: dict = {}

    def put(name: str, value: Any) -> None:
        if value is None:
            return
        text = str(value).strip() if name in VERBATIM_FIELDS else clean_text(value)
        if text and name in template:
            out[name] = text

    def put_first(names: tuple, value: Any) -> None:
        for name in names:
            if name in template:
                put(name, value)
                return

    creators = [creator_from_author(a) for a in split_authors_list(_get(row, "author"))]
    if item_type in EDITOR_TYPES:
        creators += [
            creator_from_author(a, "editor") for a in split_authors_list(_get(row, "secondary_author"))
        ]
    out["creators"] = creators

    put("title", _get(row, "title"))
    pub_year = _get(row, "pub_year")
    put("date", str(pub_year) if pub_year else "")
    put_first(SECONDARY_TITLE_FIELDS, _get(row, "secondary_title"))
    put("volume", _get(row, "volume"))
    put("issue", _get(row, "issue"))
    put("pages", _get(row, "pages"))
    put_first(PUBLISHER_FIELDS, _get(row, "publisher"))
    put("place", _get(row, "place_published"))
    put("edition", _get(row, "edition"))
    put("abstractNote", _get(row, "abstract"))
    put("section", _get(row, "news_section"))
    if item_type == "thesis":
        put("thesisType", _get(row, "type_of_work"))

    isbn_issn = norm_text(_get(row, "isbn_issn"))
    if isbn_issn:
        digits = re.sub(r"[^0-9Xx]", "", isbn_issn)
        put("ISSN" if len(digits) <= 8 else "ISBN", isbn_issn)

    doi = norm_text(_get(row, "doi"))
    if doi:
        if "DOI" in template:
            out["DOI"] = doi
        else:
            out["_extra_doi"] = doi  # merged into Extra by apply_fields()

    pdf_link, url = resolve_pdf_and_url(
        _get(row, "pub_number"), _get(row, "online_linkage"), _get(row, "online_pdf"), _get(row, "pdf")
    )
    put("url", url)
    if pdf_link:
        out["_pdf"] = pdf_link
    return out


def convert_type(old_data: dict, template: dict) -> dict:
    """Re-shape an item onto another item type's template, keeping every
    field the new type still has (same rule as Zotero's own "Change Item
    Type" and db2bibtex.zotero_item_type_fix.build_new_data)."""
    new_data = dict(template)
    for name, value in old_data.items():
        if name != "itemType" and name in new_data:
            new_data[name] = value
    for name in ("key", "version", "parentItem"):
        if name in old_data:
            new_data[name] = old_data[name]
    return new_data


def apply_fields(data: dict, wanted: dict, pub_number: Any) -> list:
    """Apply database-owned values to an item's data in place.

    Args:
        data: The item's data dict (already on the target item type).
        wanted: db_fields() output, with ``creators`` already resolved.
        pub_number: Source pub_number, for the pub_number:N tag.

    Returns:
        Names of the fields that actually changed.
    """
    changed = []
    for name, value in wanted.items():
        if name.startswith("_"):
            continue
        if name == "creators":
            if value and creators_key(value) != creators_key(data.get("creators", [])):
                data["creators"] = value
                changed.append("creators")
            continue
        if not same_value(name, data.get(name), value):
            data[name] = value
            changed.append(name)

    if wanted.get("_extra_doi") and item_doi(data) != norm_doi(wanted["_extra_doi"]):
        data["extra"] = set_extra_doi(data.get("extra", ""), wanted["_extra_doi"])
        changed.append("extra (DOI)")

    if pub_number:
        tag = PUB_NUMBER_TAG.format(pub_number)
        tags = data.setdefault("tags", [])
        if not any(t.get("tag") == tag for t in tags):
            tags.append({"tag": tag})
            changed.append("tags")
    return changed


def note_html(note_text: str) -> str:
    """Wrap Source DB note text the way Zotero's importers store it."""
    return f"<p>{html.escape(note_text, quote=False)}</p>"


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------

@dataclass
class Library:
    """One scan of the Zotero library, indexed for matching."""

    parents: dict = field(default_factory=dict)  # key -> data
    pub_ids_by_parent: dict = field(default_factory=dict)  # parent key -> [publication_id, ...]
    attachment_urls: dict = field(default_factory=dict)  # parent key -> {url, ...}


def _fetch_page(zot, start: int, progress: Callable[[str], None]) -> list:
    """One page of items at an explicit offset, retried on failure.

    A full scan is hundreds of requests, and the Zotero API returns the odd
    transient 502/503 -- one of those must not throw the whole scan away.
    Offsets (not pyzotero's follow()) make a retry stateless.
    """
    for attempt in range(PAGE_RETRIES + 1):
        try:
            return zot.items(limit=PAGE_SIZE, start=start, sort="dateAdded", direction="asc")
        except Exception as exc:  # noqa: BLE001 -- retried, then re-raised
            if attempt == PAGE_RETRIES:
                raise
            wait = RETRY_BASE_SEC * 2**attempt
            progress(f"  Zotero request at offset {start} failed ({str(exc).strip().splitlines()[0][:80]}); retrying in {wait:.0f}s...")
            time.sleep(wait)
    return []  # pragma: no cover -- loop always returns or raises


def _paginate(zot, progress: Callable[[str], None]):
    """Yield every item, page by page, oldest first (so items added while
    the scan runs land at the end instead of shifting earlier pages),
    reporting progress every ten pages."""
    start = 0
    total = "?"
    pages = 0
    while True:
        page = _fetch_page(zot, start, progress)
        if start == 0:
            request = getattr(zot, "request", None)
            headers = getattr(request, "headers", None) or {}
            total = headers.get("Total-Results", "?") if hasattr(headers, "get") else "?"
        yield from page
        start += len(page)
        pages += 1
        if pages % 10 == 0:
            progress(f"  scanned {start} of {total} Zotero items...")
        if len(page) < PAGE_SIZE:
            return


def scan_library(zot, progress: Callable[[str], None] = lambda _msg: None) -> Library:
    """Pull every item (parents and children) in one paginated scan.

    Children of trashed parents are still returned by the API (only the
    trashed parents themselves are excluded), so a large, un-emptied trash
    makes this scan much slower -- those orphans are ignored for matching,
    since every lookup below goes through a live parent's key.
    """
    lib = Library()
    for item in _paginate(zot, progress):
        data = item["data"]
        item_type = data.get("itemType")
        parent = data.get("parentItem")
        if item_type == "note":
            pub_id = extract_pub_id(data.get("note", "") or "")
            if parent and pub_id:
                ids = lib.pub_ids_by_parent.setdefault(parent, [])
                if pub_id not in ids:
                    ids.append(pub_id)
        elif item_type == "attachment":
            if parent and data.get("url"):
                lib.attachment_urls.setdefault(parent, set()).add(data["url"])
        elif item_type != "annotation":
            lib.parents[data["key"]] = data
    return lib


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def find_db_duplicates(rows_by_id: dict) -> list:
    """Database rows that share a DOI or a title + year -- each pair would
    import as a Zotero duplicate. Reported for cleanup in the source DB.

    Returns:
        [(match_on, value, [publication_ids], [titles])].
    """
    groups: dict = {}
    for pid, row in rows_by_id.items():
        doi = norm_doi(_get(row, "doi"))
        if doi:
            groups.setdefault(("DOI", doi), []).append(pid)
        title = norm_title(_get(row, "title"))
        if title:
            groups.setdefault(("title + year", f"{title}|{year_of(_get(row, 'pub_year'))}"), []).append(pid)
    out = []
    for (match_on, value), pids in groups.items():
        if len(pids) > 1:
            pids = sorted(pids, key=int)
            titles = [norm_text(_get(rows_by_id[p], "title"))[:120] for p in pids]
            shown = value if match_on == "DOI" else titles[0]
            out.append((match_on, shown, pids, titles))
    return sorted(out, key=lambda g: int(g[2][0]))


def match_unlinked(lib: Library, rows_by_id: dict, linked_ids: set) -> "tuple[dict, list]":
    """Match Zotero items with no Source DB note to database rows.

    A match needs the same DOI, or (no DOI on either side) the same title
    and year, and must be unique in both directions. A Zotero item matching
    a row that's already linked to another Zotero item is a duplicate of
    that item -- reported, never adopted.

    Returns:
        ({zotero_key: publication_id} to adopt, [ZoteroOnly]).
    """
    by_doi: dict = {}
    by_title: dict = {}
    for pid, row in rows_by_id.items():
        doi = norm_doi(_get(row, "doi"))
        if doi:
            by_doi.setdefault(doi, []).append(pid)
        title = norm_title(_get(row, "title"))
        if title:
            by_title.setdefault((title, year_of(_get(row, "pub_year"))), []).append(pid)

    adopt: dict = {}
    claimed: dict = {}  # pid -> zotero key that adopted it
    only = []
    for key, data in sorted(lib.parents.items()):
        if key in lib.pub_ids_by_parent:
            continue
        doi = item_doi(data)
        title = data.get("title", "")
        year = year_of(data.get("date"))
        candidates = by_doi.get(doi, []) if doi else []
        match_on = "DOI"
        if not candidates:
            candidates = by_title.get((norm_title(title), year), [])
            match_on = "title + year"

        def report(reason: str) -> None:
            only.append(ZoteroOnly(key, norm_text(title)[:120], year, doi, reason))

        if not candidates:
            report("no matching database record -- kept")
        elif len(candidates) > 1:
            report(f"{match_on} matches several database records ({', '.join(sorted(candidates, key=int))}) -- kept, resolve by hand")
        elif candidates[0] in linked_ids:
            report(f"duplicate of publication_id {candidates[0]} already in Zotero ({match_on}) -- merge in Zotero")
        elif candidates[0] in claimed:
            report(f"same {match_on} as {claimed[candidates[0]]}, which was adopted for publication_id {candidates[0]} -- merge in Zotero")
        else:
            adopt[key] = candidates[0]
            claimed[candidates[0]] = key
    return adopt, only


class _Templates:
    """Per-run cache of zot.item_template() results."""

    def __init__(self, zot) -> None:
        self.zot = zot
        self.cache: dict = {}

    def get(self, item_type: str, linkmode: Optional[str] = None) -> dict:
        key = (item_type, linkmode)
        if key not in self.cache:
            self.cache[key] = self.zot.item_template(item_type, linkmode) if linkmode else self.zot.item_template(item_type)
        return copy.deepcopy(self.cache[key])


def resolve_creators(wanted: list, current: list, doi: str, cfg: Config) -> "tuple[list, str]":
    """Pick the creator list to write.

    Returns:
        (creators, detail) -- detail explains any "et al." handling.
    """
    if not authors_mod.is_incomplete(wanted):
        return wanted, ""
    if current and not authors_mod.is_incomplete(current):
        return current, "database authors end in 'et al.'; kept the complete Zotero list"
    if not cfg.fix_authors:
        return wanted, "database authors end in 'et al.'"
    if not doi:
        return wanted, "database authors end in 'et al.'; no DOI to look up"
    found = authors_mod.fetch_authors_crossref(doi, cfg.crossref_mailto or "")
    time.sleep(cfg.crossref_rate_limit_sec)
    if not found:
        found = authors_mod.fetch_authors_datacite(doi)
    if not found:
        return wanted, "database authors end in 'et al.'; DOI lookup found no authors"
    editors = [c for c in wanted if c.get("creatorType") == "editor"]
    return authors_mod.to_zotero_creators(found) + editors, "full author list from DOI lookup"


def plan_row(
    row: Any,
    existing: Optional[dict],
    lib: Library,
    templates: _Templates,
    cfg: Config,
    status_if_changed: str,
) -> Action:
    """Decide what to do with one database row and its Zotero item, if any."""
    pid = str(_get(row, "publication_id"))
    pub_number = _get(row, "pub_number")
    new_type = guess_zotero_type(_get(row, "reference_type"), _get(row, "pub_type"))
    template = templates.get(new_type)
    wanted = db_fields(row, template)
    note_text = build_source_note(_get(row, "notes"), pid, pub_number, _get(row, "catalog_id"))

    if existing is None:
        data = template
        old_type = ""
    else:
        old_type = existing.get("itemType", "")
        data = convert_type(existing, template) if old_type != new_type else dict(existing)
        data["tags"] = [dict(t) for t in existing.get("tags", [])]

    creators, creator_detail = resolve_creators(
        wanted["creators"], (existing or {}).get("creators", []), norm_doi(_get(row, "doi")), cfg
    )
    wanted["creators"] = creators
    changes = apply_fields(data, wanted, pub_number)
    if existing is not None and old_type != new_type:
        changes.insert(0, "itemType")

    pdf_url = wanted.get("_pdf")
    if existing is not None and pdf_url in lib.attachment_urls.get(existing["key"], set()):
        pdf_url = None
    if existing is None and cfg.collection_key:
        data["collections"] = [cfg.collection_key]

    action = Action(
        status="create" if existing is None else status_if_changed,
        publication_id=pid,
        pub_number=str(pub_number or ""),
        item_key=(existing or {}).get("key", ""),
        title=norm_text(_get(row, "title"))[:120],
        old_item_type=old_type,
        new_item_type=new_type,
        changes=changes,
        detail=creator_detail,
        data=data,
        note_text=note_text if status_if_changed == "adopt" or existing is None else None,
        pdf_url=pdf_url,
    )
    if existing is not None and not changes and not action.note_text:
        if pdf_url:
            action.changes = ["PDF attachment"]
        else:
            action.status = "unchanged"
            action.data = None
    elif pdf_url and existing is not None:
        action.changes.append("PDF attachment")
    return action


def build_plan(zot, rows: list, cfg: Config, progress: Callable[[str], None]) -> SyncPlan:
    """Scan the library, match it against the database rows, and plan every
    create/update/adopt without writing anything."""
    plan = SyncPlan()
    rows_by_id: dict = {}
    for row in rows:
        if not _get(row, "title") or not _get(row, "author"):
            plan.skipped_rows.append(str(_get(row, "publication_id")))
            continue
        rows_by_id[str(_get(row, "publication_id"))] = row

    progress(
        "Scanning the Zotero library (items, notes, attachments) -- several minutes; "
        "much longer if the Zotero trash hasn't been emptied..."
    )
    lib = scan_library(zot, progress)
    progress(
        f"Zotero: {len(lib.parents)} items, {len(lib.pub_ids_by_parent)} with a Source DB publication_id"
    )

    # Linked items, by publication_id. Several Zotero items carrying the same
    # publication_id are duplicates: the oldest is synced, the rest reported.
    linked: dict = {}
    for key, pids in lib.pub_ids_by_parent.items():
        if key in lib.parents:
            for pid in pids:
                linked.setdefault(pid, []).append(lib.parents[key])
    for pid, items in linked.items():
        items.sort(key=lambda d: d.get("dateAdded", ""))
        for dup in items[1:]:
            plan.actions.append(
                Action(
                    "zotero-duplicate",
                    publication_id=pid,
                    item_key=dup["key"],
                    title=norm_text(dup.get("title"))[:120],
                    old_item_type=dup.get("itemType", ""),
                    detail=f"same publication_id as {items[0]['key']} -- merge in Zotero",
                )
            )

    adopt, plan.zotero_only = match_unlinked(lib, rows_by_id, set(linked))
    adopt_by_pid = {pid: key for key, pid in adopt.items()}
    plan.db_duplicates = find_db_duplicates(rows_by_id)

    templates = _Templates(zot)
    progress(f"Planning {len(rows_by_id)} database records...")
    for pid, row in rows_by_id.items():
        if pid in linked:
            item = linked[pid][0]
            # An item merged in Zotero from two records that are both still
            # in the query: syncing it from either would flip it back and
            # forth, and creating the other would undo the merge.
            live = [p for p in lib.pub_ids_by_parent[item["key"]] if p in rows_by_id]
            if len(live) > 1:
                others = ", ".join(p for p in live if p != pid)
                action = Action(
                    "merged-duplicate",
                    publication_id=pid,
                    pub_number=str(_get(row, "pub_number") or ""),
                    item_key=item["key"],
                    title=norm_text(_get(row, "title"))[:120],
                    old_item_type=item.get("itemType", ""),
                    detail=f"Zotero item also carries publication_id {others} (merged duplicates) -- "
                    "left as is; remove or unpublish the duplicate record in the database",
                )
            else:
                action = plan_row(row, item, lib, templates, cfg, "update")
        elif pid in adopt_by_pid:
            action = plan_row(row, lib.parents[adopt_by_pid[pid]], lib, templates, cfg, "adopt")
        else:
            action = plan_row(row, None, lib, templates, cfg, "create")
        plan.actions.append(action)

    for pid, items in linked.items():
        if pid not in rows_by_id:
            plan.actions.append(
                Action(
                    "not-in-query",
                    publication_id=pid,
                    item_key=items[0]["key"],
                    title=norm_text(items[0].get("title"))[:120],
                    old_item_type=items[0].get("itemType", ""),
                    detail="publication_id not returned by the query (unpublished, out of scope, or deleted) -- kept",
                )
            )
    return plan


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _post(zot, payload: list) -> "list[tuple[bool, str]]":
    """create_items() in batches; returns (ok, key-or-error) per object."""
    results = []
    for start in range(0, len(payload), BATCH_SIZE):
        chunk = payload[start : start + BATCH_SIZE]
        try:
            resp = zot.create_items(chunk)
        except Exception as exc:  # noqa: BLE001 -- whole batch failed; surfaced per item
            results.extend((False, str(exc)) for _ in chunk)
            continue
        successful = resp.get("successful", {}) or {}
        unchanged = resp.get("unchanged", {}) or {}
        failed = resp.get("failed", {}) or {}
        for i, obj in enumerate(chunk):
            idx = str(i)
            if idx in successful:
                results.append((True, successful[idx].get("key", obj.get("key", ""))))
            elif idx in unchanged:
                results.append((True, unchanged[idx] if isinstance(unchanged[idx], str) else obj.get("key", "")))
            else:
                err = failed.get(idx, {})
                results.append((False, f"{err.get('code', '')} {err.get('message', 'no result returned')}".strip()))
    return results


def apply_plan(zot, plan: SyncPlan, cfg: Config, progress: Callable[[str], None]) -> None:
    """Write the plan's creates/updates/adopts, then their child notes and
    PDF attachments. Updates each Action's status in place."""
    todo = [a for a in plan.actions if a.status in ("create", "update", "adopt")]
    if cfg.limit is not None:
        for a in todo[cfg.limit :]:
            a.detail = (a.detail + "; " if a.detail else "") + f"not written (--limit {cfg.limit})"
        todo = todo[: cfg.limit]

    done = {"create": "created", "update": "updated", "adopt": "adopted"}
    parents = [a for a in todo if a.data is not None]
    progress(f"Writing {len(parents)} item(s) to Zotero...")
    for a, (ok, info) in zip(parents, _post(zot, [a.data for a in parents])):
        if ok:
            a.item_key = info
            a.status = done[a.status]
        else:
            a.detail = (a.detail + "; " if a.detail else "") + info
            a.status = "error"

    templates = _Templates(zot)
    children: list = []
    owners: list = []
    for a in todo:
        if a.status not in done.values():
            continue
        if a.note_text:
            note = templates.get("note")
            note["note"] = note_html(a.note_text)
            note["parentItem"] = a.item_key
            children.append(note)
            owners.append(a)
        if a.pdf_url:
            att = templates.get("attachment", "linked_url")
            att.update(
                {"title": a.pdf_url.rstrip("/").rsplit("/", 1)[-1], "url": a.pdf_url, "contentType": "application/pdf", "parentItem": a.item_key}
            )
            children.append(att)
            owners.append(a)
    if children:
        progress(f"Adding {len(children)} child note(s)/PDF link(s)...")
        for a, (ok, info) in zip(owners, _post(zot, children)):
            if not ok:
                a.detail = (a.detail + "; " if a.detail else "") + f"child item failed: {info}"


# ---------------------------------------------------------------------------
# Audit workbook
# ---------------------------------------------------------------------------

def _require_openpyxl() -> None:
    if Workbook is None:
        raise ZoteroDepsMissingError(
            "openpyxl is required to write the audit workbook but is not "
            "importable. Install it with: pip install -e '.[zotero]'"
        )


def _sheet(wb, title: str, headers: list, rows: list, widths: list, first: bool = False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for r in rows:
        ws.append(r)
    for i, width in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = width
    return ws


def write_audit_xlsx(plan: SyncPlan, path: str, dry_run: bool) -> None:
    """Write the actions / zotero_only / db_duplicates / summary sheets."""
    _require_openpyxl()
    wb = Workbook()
    order = {s: i for i, s in enumerate(
        ["error", "create", "created", "adopt", "adopted", "update", "updated",
         "merged-duplicate", "zotero-duplicate", "not-in-query", "unchanged"]
    )}
    actions = sorted(plan.actions, key=lambda a: (order.get(a.status, 99), int(a.publication_id or 0)))
    _sheet(
        wb,
        "actions",
        ["status", "publication_id", "pub_number", "item_key", "title", "old_item_type",
         "new_item_type", "changed_fields", "detail"],
        [
            [a.status, int(a.publication_id) if a.publication_id.isdigit() else a.publication_id,
             a.pub_number, a.item_key, a.title, a.old_item_type, a.new_item_type,
             ", ".join(a.changes), a.detail]
            for a in actions
        ],
        [16, 14, 12, 12, 50, 16, 16, 40, 50],
        first=True,
    )
    _sheet(
        wb,
        "zotero_only",
        ["item_key", "title", "year", "doi", "reason"],
        [[z.item_key, z.title, z.year, z.doi, z.reason] for z in plan.zotero_only],
        [12, 60, 8, 30, 70],
    )
    _sheet(
        wb,
        "db_duplicates",
        ["match_on", "value", "publication_ids", "titles"],
        [[m, v, ", ".join(p), " | ".join(t)] for m, v, p, t in plan.db_duplicates],
        [14, 50, 24, 90],
    )
    summary = [["mode", "dry run (nothing written)" if dry_run else "live"]]
    summary += [[k, v] for k, v in sorted(plan.counts().items())]
    summary.append(["database duplicate groups", len(plan.db_duplicates)])
    if plan.skipped_rows:
        summary.append(["database rows skipped (no title/author)", ", ".join(plan.skipped_rows)])
    _sheet(wb, "summary", ["item", "value"], summary, [40, 60])
    wb.save(path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _require_zotero() -> None:
    if zotero is None:
        raise ZoteroDepsMissingError(
            "pyzotero is required but is not importable. Install it with: pip install -e '.[zotero]'"
        )


def get_client(cfg: Config):
    """Build a pyzotero client from a Config."""
    _require_zotero()
    return zotero.Zotero(cfg.library_id, cfg.library_type, cfg.api_key)


def fetch_rows(cfg: Config) -> list:
    """Run query.sql (see db2bibtex.exporter.fetch_rows)."""
    return exporter.fetch_rows(
        cfg.server,
        cfg.database,
        cfg.before_year,
        cfg.query_file,
        driver=cfg.driver,
        trusted_connection=cfg.trusted_connection,
        uid=cfg.uid,
        pwd=cfg.pwd,
        trust_server_certificate=cfg.trust_server_certificate,
    )


def run(cfg: Config, progress_callback: Callable[[str], None] = print) -> SyncPlan:
    """Plan the sync, write it unless dry_run, and write the audit workbook.
    Used by the CLI, the GUI and tests."""
    zot = get_client(cfg)
    _require_openpyxl()

    progress_callback(f"Querying {cfg.server}/{cfg.database} ({cfg.query_file}, pub_year < {cfg.before_year})...")
    rows = fetch_rows(cfg)
    progress_callback(f"Database: {len(rows)} records")

    plan = build_plan(zot, rows, cfg, progress_callback)
    progress_callback(f"Plan: {plan.counts()}")
    if plan.db_duplicates:
        progress_callback(
            f"{len(plan.db_duplicates)} group(s) of database records share a DOI or title -- "
            "see the db_duplicates sheet"
        )

    if not cfg.dry_run:
        apply_plan(zot, plan, cfg, progress_callback)
        progress_callback(f"Done: {plan.counts()}")

    write_audit_xlsx(plan, cfg.audit_path, cfg.dry_run)
    progress_callback(f"Wrote audit workbook {cfg.audit_path}")
    if cfg.dry_run:
        progress_callback("Dry run only -- nothing was written. Review the audit, then rerun with --live.")
    return plan
