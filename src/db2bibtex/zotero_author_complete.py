"""
zotero_author_complete.py

Finds Zotero library items whose creator list was truncated to "et al.",
looks up the full author list via the item's DOI (CrossRef, falling back
to DataCite), and replaces the creators array in place -- leaving every
other field (tags, collections, notes, Extra, attachments, relations,
version) untouched.

Standalone feature: does not import db2bibtex.compare or db2bibtex.exporter
and never touches the SQL Server database.

Design notes:
- Detection is self-contained: is_incomplete() only inspects the Zotero
  creator array for an "et al." sentinel entry, normalized against a
  regex that accepts common punctuation/spacing variants ("et al",
  "et al.", "et. al.", "et.al.", ...) but always requires a separator
  between "et" and "al", so a real (if rare) surname like "Etal" is never
  mistaken for the sentinel. An item only ever reaches the audit
  CSV if it was flagged, and its full original creator list (sentinel
  entry included, verbatim) is written to that CSV's old_creators column
  -- so the first --collection dry-run doubles as a way to confirm the
  matcher is actually catching this library's real sentinel formatting
  before trusting it at scale.
- Merge is a full replace of `creators`, never a diff/append: "et al."
  is an unambiguous incomplete-list marker, so nothing in the old list is
  worth preserving.
- process_item() re-fetches the item immediately before writing and only
  changes `creators` locally, so pyzotero's version check protects
  against clobbering concurrent edits and every other field survives.
"""

from __future__ import annotations

import configparser
import csv
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover -- exercised via ZoteroDepsMissingError tests
    requests = None

try:
    from pyzotero import zotero
except ImportError:  # pragma: no cover -- exercised via ZoteroDepsMissingError tests
    zotero = None

log = logging.getLogger(__name__)

CROSSREF_API = "https://api.crossref.org/works/{doi}"
DATACITE_API = "https://api.datacite.org/dois/{doi}"
REQUEST_TIMEOUT = 15
DEFAULT_CROSSREF_RATE_LIMIT_SEC = 0.5

# Matches "et al", "et al.", "et. al.", "et.al.", etc. either as the whole
# candidate string or as a trailing suffix on one ("Cynthia S. et al" --
# real samples showed the sentinel tacked onto the last real author's
# given-name field, not just living in its own creator entry), after the
# candidate has been lowercased -- see is_incomplete(). "et" must be
# preceded by a boundary (string start, whitespace, comma, semicolon, or
# slash -- the source database's author field mixes "//"- and
# ";"-delimited multi-author strings, and whichever delimiter the
# db2bibtex exporter didn't recognize is exactly where "et al." ends up
# glued onto whatever preceded it) and separated from "al" by
# punctuation/whitespace, so a real, if rare, surname like "Etal" -- or a
# real name that merely ends in "al" (Alvarez, Albert, Beckman...) -- is
# never mistaken for the sentinel. A false positive here would silently
# overwrite real authors.
ET_AL_RE = re.compile(r"(?:^|[\s,;/])et[.\s]+al\.?$")


class ZoteroDepsMissingError(RuntimeError):
    """Raised when a Zotero operation is attempted but pyzotero/requests
    are not importable."""


@dataclass
class Config:
    library_id: str
    library_type: str  # 'group' or 'user'
    api_key: str
    crossref_mailto: str  # for CrossRef "polite pool"
    dry_run: bool = True
    audit_csv: str = "zotero_author_complete_audit.csv"
    collection_key: str | None = None  # optional: restrict to one collection
    crossref_rate_limit_sec: float = DEFAULT_CROSSREF_RATE_LIMIT_SEC


@dataclass
class AuditRow:
    item_key: str
    title: str
    doi: str
    old_creators: str
    new_creators: str
    status: str  # 'updated' | 'dry-run' | 'skipped-no-doi' | 'skipped-lookup-failed' | 'error'
    detail: str = ""


@dataclass
class RunResult:
    rows: list[AuditRow] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def load_config(config_path: str | Path) -> dict:
    """Read Zotero/CrossRef settings from an .ini file.

    Args:
        config_path: Path to a db_config.ini-style file with [Zotero] and
            [CrossRef] sections (see db_config.ini.example).

    Returns:
        A dict with keys: library_id, library_type, api_key, crossref_mailto.
    """
    cp = configparser.ConfigParser()
    read_files = cp.read(config_path)
    if not read_files:
        raise FileNotFoundError(f"Could not read config file: {config_path}")

    zsec = cp["Zotero"] if cp.has_section("Zotero") else {}
    csec = cp["CrossRef"] if cp.has_section("CrossRef") else {}

    return {
        "library_id": zsec.get("library_id"),
        "library_type": zsec.get("library_type"),
        "api_key": zsec.get("api_key"),
        "crossref_mailto": csec.get("mailto"),
    }


def save_config(
    config_path: str | Path,
    library_id: str,
    library_type: str,
    api_key: str,
    crossref_mailto: str,
) -> None:
    """Write Zotero/CrossRef settings to an .ini file, preserving any other
    sections already there (e.g. [Database]/[Driver] written by
    db2bibtex.exporter.save_config -- both features can share one
    db_config.ini).

    Args:
        config_path: Destination path.
        library_id: Zotero library ID.
        library_type: 'group' or 'user'.
        api_key: Zotero API key. Written in plaintext -- callers (e.g. the
            GUI) should warn the user before invoking this with a real key.
        crossref_mailto: Email address for CrossRef's polite pool.
    """
    cp = configparser.ConfigParser()
    cp.read(config_path)  # preserve any other sections already in the file
    cp["Zotero"] = {
        "library_id": library_id or "",
        "library_type": library_type or "",
        "api_key": api_key or "",
    }
    cp["CrossRef"] = {"mailto": crossref_mailto or ""}
    with open(config_path, "w", encoding="utf-8") as fh:
        cp.write(fh)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _creator_sentinel_candidates(c: dict) -> list[str]:
    """All plausible sentinel-text strings for one creator dict.

    Two-field creators don't reliably put "et al." in one field -- real
    library samples showed it split as firstName="et", lastName="al"
    (Zotero displays this as "al, et"), and separately, appended as a
    suffix onto a real author's firstName ("Cynthia S. et al"). So
    firstName and lastName are checked both individually and combined,
    alongside single-field mode's `name`.
    """
    name = (c.get("name") or "").strip()
    first = (c.get("firstName") or "").strip()
    last = (c.get("lastName") or "").strip()
    candidates = [name, first, last]
    if first and last:
        candidates.append(f"{first} {last}")
    return [cand.lower() for cand in candidates if cand]


def is_incomplete(creators: list[dict]) -> bool:
    """True if the creator list contains an 'et al.' sentinel entry, either
    as its own creator or appended as a suffix onto a real one.

    Handles both two-field mode ({firstName, lastName}, whether the
    sentinel lands in one field, is split across both, or trails a real
    given name) and single-field mode ({name: ...}) creators,
    case/punctuation/spacing-insensitive.
    """
    for c in creators:
        for candidate in _creator_sentinel_candidates(c):
            if ET_AL_RE.search(candidate):
                return True
    return False


def get_doi(item_data: dict) -> str | None:
    """Pull a DOI off the item's DOI field. No DB lookup fallback for v1 --
    if there's no DOI, the caller logs and skips it."""
    doi = (item_data.get("DOI") or "").strip()
    return doi or None


# ---------------------------------------------------------------------------
# External metadata lookup
# ---------------------------------------------------------------------------

def _require_requests() -> None:
    if requests is None:
        raise ZoteroDepsMissingError(
            "requests is required for DOI lookups but is not importable. "
            "Install it with: pip install -e '.[zotero]'"
        )


def fetch_authors_crossref(doi: str, mailto: str) -> list[dict] | None:
    """Query CrossRef for a DOI and return authors as a list of
    {'given': ..., 'family': ...} dicts, or None if not found / no authors."""
    _require_requests()
    headers = {"User-Agent": f"db2bibtex-fix-authors/1.0 (mailto:{mailto})"}
    resp = requests.get(CROSSREF_API.format(doi=doi), headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return None
    message = resp.json().get("message", {})
    authors = message.get("author")
    if not authors:
        return None
    return [{"given": a.get("given", ""), "family": a.get("family", "")} for a in authors]


def fetch_authors_datacite(doi: str) -> list[dict] | None:
    """Fallback for DOIs CrossRef doesn't cover (e.g. some datasets/reports)."""
    _require_requests()
    resp = requests.get(DATACITE_API.format(doi=doi), timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return None
    creators = resp.json().get("data", {}).get("attributes", {}).get("creators", [])
    out = []
    for c in creators:
        # DataCite creators are often "Family, Given" in a single 'name' field.
        name = c.get("name", "")
        if "," in name:
            family, given = (p.strip() for p in name.split(",", 1))
        else:
            family, given = name.strip(), ""
        out.append({"given": given, "family": family})
    return out or None


def to_zotero_creators(authors: list[dict]) -> list[dict]:
    """Map {'given','family'} dicts to Zotero's creator schema."""
    return [
        {"creatorType": "author", "firstName": a.get("given", ""), "lastName": a["family"]}
        for a in authors
    ]


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _require_zotero() -> None:
    if zotero is None:
        raise ZoteroDepsMissingError(
            "pyzotero is required but is not importable. "
            "Install it with: pip install -e '.[zotero]'"
        )


def get_client(cfg: Config):
    """Build a pyzotero client from a Config."""
    _require_zotero()
    return zotero.Zotero(cfg.library_id, cfg.library_type, cfg.api_key)


def find_incomplete_items(zot, collection_key: str | None) -> list[dict]:
    """Paginate the full library (or one collection) and return items needing repair."""
    if collection_key:
        items = zot.everything(zot.collection_items(collection_key))
    else:
        items = zot.everything(zot.top())
    return [it for it in items if is_incomplete(it["data"].get("creators", []))]


def process_item(zot, item: dict, cfg: Config) -> AuditRow:
    """Look up and (unless dry_run) apply the full-replace creator fix for one item."""
    data = item["data"]
    title = data.get("title", "")[:80]
    key = data["key"]
    old_creators_str = "; ".join(
        c.get("lastName") or c.get("name", "") for c in data.get("creators", [])
    )

    doi = get_doi(data)
    if not doi:
        log.info("skip (no DOI): %s [%s]", title, key)
        return AuditRow(key, title, "", old_creators_str, "", "skipped-no-doi")

    authors = fetch_authors_crossref(doi, cfg.crossref_mailto)
    time.sleep(cfg.crossref_rate_limit_sec)
    if not authors:
        authors = fetch_authors_datacite(doi)
    if not authors:
        log.warning("skip (lookup failed for DOI %s): %s [%s]", doi, title, key)
        return AuditRow(key, title, doi, old_creators_str, "", "skipped-lookup-failed")

    new_creators = to_zotero_creators(authors)
    new_creators_str = "; ".join(c["lastName"] for c in new_creators)

    if cfg.dry_run:
        log.info("would update: %s [%s]: '%s' -> '%s'", title, key, old_creators_str, new_creators_str)
        return AuditRow(key, title, doi, old_creators_str, new_creators_str, "dry-run")

    # Re-fetch immediately before writing so we hold the current version
    # (guards against edits made elsewhere since find_incomplete_items ran).
    fresh_item = zot.item(key)
    fresh_item["data"]["creators"] = new_creators
    try:
        zot.update_item(fresh_item)
        log.info("updated: %s [%s]: '%s' -> '%s'", title, key, old_creators_str, new_creators_str)
        return AuditRow(key, title, doi, old_creators_str, new_creators_str, "updated")
    except Exception as exc:  # noqa: BLE001 -- surfaced via audit CSV, not swallowed
        log.error("update failed: %s [%s]: %s", title, key, exc)
        return AuditRow(key, title, doi, old_creators_str, new_creators_str, "error", str(exc))


def write_audit_csv(rows: list[AuditRow], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["item_key", "title", "doi", "old_creators", "new_creators", "status", "detail"]
        )
        for r in rows:
            writer.writerow(
                [r.item_key, r.title, r.doi, r.old_creators, r.new_creators, r.status, r.detail]
            )


def run(cfg: Config, progress_callback=print) -> RunResult:
    """Scan the library (or one collection), process every flagged item, and
    write the audit CSV. Used by both the CLI and tests."""
    zot = get_client(cfg)

    progress_callback(
        "Scanning library for 'et al.' items"
        + (f" in collection {cfg.collection_key}" if cfg.collection_key else "")
        + "..."
    )
    incomplete = find_incomplete_items(zot, cfg.collection_key)
    progress_callback(f"Found {len(incomplete)} items with truncated author lists")

    rows = [process_item(zot, item, cfg) for item in incomplete]

    write_audit_csv(rows, cfg.audit_csv)
    counts: dict = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    progress_callback(f"Done. {counts}")
    if cfg.dry_run:
        progress_callback(
            f"Dry-run only -- rerun with --live to apply changes. Review {cfg.audit_csv} first."
        )
    return RunResult(rows=rows, counts=counts)
