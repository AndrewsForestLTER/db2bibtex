"""Command-line interface for db2bibtex's compare feature.

Compares a database BibTeX export against a Zotero library BibTeX export
and writes the entries present in the database export but missing from the
library (matched by ``publication_id``, see compare.py) to a new file ready
for Zotero import.
"""

from __future__ import annotations

import argparse
import sys

from db2bibtex.compare import find_missing, write_missing


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the db2bibtex-compare CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Compare a database BibTeX export against a Zotero library "
            "BibTeX export and write the entries missing from the library "
            "(matched by publication_id) to a new file ready for import."
        )
    )
    parser.add_argument(
        "--backup",
        required=True,
        help="Path to the database BibTeX export (e.g. from db2bibtex)",
    )
    parser.add_argument(
        "--library",
        required=True,
        help="Path to a Zotero library BibTeX export (Zotero: File -> "
        "Export Library -> BibTeX)",
    )
    parser.add_argument(
        "--output",
        default="missing_from_library.bib",
        help="Output .bib path for entries missing from the library "
        "(default missing_from_library.bib)",
    )
    return parser


def main(argv: list | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    missing_ids, backup_ids, library_ids, backup_unparsed = find_missing(
        args.backup, args.library
    )
    write_missing(missing_ids, backup_ids, args.output, args.backup, args.library)

    print(
        f"Backup: {len(backup_ids)} entries with a parseable publication_id "
        f"({len(backup_unparsed)} without)"
    )
    print(f"Library: {len(library_ids)} entries with a parseable publication_id")
    print(f"Missing from library: {len(missing_ids)} entries -> wrote {args.output}")

    if backup_unparsed:
        print(
            f"WARNING: {len(backup_unparsed)} backup entries had no parseable "
            "publication_id and were skipped (not counted, not written):",
            file=sys.stderr,
        )
        for entry in backup_unparsed:
            first_line = entry.strip().splitlines()[0] if entry.strip() else "(empty entry)"
            print(f"  {first_line}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
