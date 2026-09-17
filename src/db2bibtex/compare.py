"""
compare.py

Compares a BibTeX export from the source database against a BibTeX export
of a Zotero library, to find entries present in the database export but
missing from the Zotero library.

Matching is done on the ``publication_id`` embedded in each entry's `note`
field (``Source DB: publication_id X; pub_number Y; catalog_id Z``), not on
citation key or the `keywords` tag. Citation key format changed partway
through this project's history (a heuristic Author+Year+Title scheme was
replaced by ``AND<pub_number>``), and the `keywords` tag containing
pub_number was added later still, so a live Zotero library may contain
items imported under several different historical exporter versions. The
`note` field's ``publication_id`` is the one identifier that has been
written in every version of the exporter, making it the only reliable join
key across mixed-vintage library items.
"""

from __future__ import annotations

import re

ENTRY_START_RE = re.compile(r"^@\w+\{", re.MULTILINE)
# exporter.escape_bibtex() escapes every underscore in free text, so a real
# note field reads "publication\_id 2075", not "publication_id 2075" -- the
# backslash is optional here so this matches both real (escaped) output and
# any hand-edited or future-format (unescaped) .bib content.
PUB_ID_RE = re.compile(r"publication\\?_id\s+(\d+)")


def split_entries(bib_text):
    """Split raw bibtex text into a list of individual entry strings."""
    starts = [m.start() for m in ENTRY_START_RE.finditer(bib_text)]
    if not starts:
        return []
    starts.append(len(bib_text))
    return [bib_text[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]


def extract_pub_id(entry_text):
    m = PUB_ID_RE.search(entry_text)
    return m.group(1) if m else None


def load_pub_ids(path):
    """Return (dict of pub_id -> entry_text, list of entries with no
    parseable pub_id)."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    entries = split_entries(text)
    by_id = {}
    unparsed = []
    for entry in entries:
        pub_id = extract_pub_id(entry)
        if pub_id:
            by_id[pub_id] = entry
        else:
            unparsed.append(entry)
    return by_id, unparsed


def find_missing(backup_path, library_path):
    """Returns (missing_ids: sorted list of str, backup_ids: dict,
    library_ids: dict, backup_unparsed: list) -- missing_ids are pub_ids
    present in backup but not in library, sorted numerically."""
    backup_ids, backup_unparsed = load_pub_ids(backup_path)
    library_ids, _ = load_pub_ids(library_path)
    missing_ids = sorted(
        (pid for pid in backup_ids if pid not in library_ids),
        key=int,
    )
    return missing_ids, backup_ids, library_ids, backup_unparsed


def write_missing(missing_ids, backup_ids, output_path, backup_path, library_path):
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"% {len(missing_ids)} entries present in {backup_path} "
                 f"but not found (by publication_id) in {library_path}\n\n")
        for pid in missing_ids:
            f.write(backup_ids[pid])
            f.write("\n\n")
