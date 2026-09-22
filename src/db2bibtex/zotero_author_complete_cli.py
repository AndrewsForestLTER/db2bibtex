"""Command-line interface for db2bibtex's fix-authors feature.

Scans a Zotero library for items whose creator list was truncated to
"et al.", looks up the full author list via CrossRef/DataCite using the
item's DOI, and replaces the creators in place. Runs independently of the
compare/export features -- no shared state, config, or sequencing
dependency with them.
"""

from __future__ import annotations

import argparse
import logging
import sys

from db2bibtex.zotero_author_complete import Config, ZoteroDepsMissingError, load_config, run


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the db2bibtex-fix-authors CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Scan a Zotero library for items with a truncated 'et al.' "
            "creator list, look up the full author list by DOI (CrossRef, "
            "then DataCite), and replace the creators in place. Dry-run by "
            "default; pass --live to actually write changes."
        )
    )
    parser.add_argument(
        "--config",
        help="Path to an .ini config file with [Zotero]/[CrossRef] sections "
        "(recommended -- keeps the API key out of shell history; see "
        "db_config.ini.example)",
    )
    parser.add_argument("--library-id", help="Zotero library ID (overrides config)")
    parser.add_argument(
        "--library-type", choices=["user", "group"], help="Zotero library type (overrides config)"
    )
    parser.add_argument("--api-key", help="Zotero API key (overrides config)")
    parser.add_argument(
        "--crossref-mailto",
        help="Email address for CrossRef's polite pool (overrides config)",
    )
    parser.add_argument(
        "--collection",
        help="Restrict to one collection key. Strongly recommended for the "
        "first run against a real library, before running across the "
        "whole thing.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually write changes to Zotero (default: dry-run, no writes)",
    )
    parser.add_argument(
        "--audit-path",
        default="zotero_author_complete_audit.xlsx",
        help="Output .xlsx audit workbook path (default "
        "zotero_author_complete_audit.xlsx)",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=0.5,
        help="Seconds to sleep between CrossRef requests (default 0.5)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser


def main(argv: list | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    file_cfg = load_config(args.config) if args.config else {}

    library_id = args.library_id or file_cfg.get("library_id")
    library_type = args.library_type or file_cfg.get("library_type")
    api_key = args.api_key or file_cfg.get("api_key")
    crossref_mailto = args.crossref_mailto or file_cfg.get("crossref_mailto")

    missing = [
        flag
        for flag, value in [
            ("--library-id", library_id),
            ("--library-type", library_type),
            ("--api-key", api_key),
            ("--crossref-mailto", crossref_mailto),
        ]
        if not value
    ]
    if missing:
        print(
            f"Missing required settings: {', '.join(missing)} "
            f"(pass directly, or add [Zotero]/[CrossRef] sections to a "
            f"--config file -- see db_config.ini.example).",
            file=sys.stderr,
        )
        return 2

    cfg = Config(
        library_id=library_id,
        library_type=library_type,
        api_key=api_key,
        crossref_mailto=crossref_mailto,
        dry_run=not args.live,
        audit_path=args.audit_path,
        collection_key=args.collection,
        crossref_rate_limit_sec=args.rate_limit,
    )

    try:
        result = run(cfg, progress_callback=print)
    except ZoteroDepsMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
        print(f"error: fix-authors run failed: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote audit log ({len(result.rows)} rows) to {cfg.audit_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
