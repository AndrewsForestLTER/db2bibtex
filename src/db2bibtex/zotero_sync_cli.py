"""Command-line interface for db2bibtex-sync.

Syncs the Zotero library directly against the source database through the
Zotero Web API: creates missing records with the right item type, updates
changed ones (database-owned fields win), adopts matching Zotero-only items,
and reports everything else. Dry-run by default -- see zotero_sync.py.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from db2bibtex.db import DEFAULT_DRIVER, PyodbcMissingError, QueryFileMissingError
from db2bibtex.zotero_sync import (
    DEFAULT_BEFORE_YEAR,
    DEFAULT_QUERY_FILE,
    Config,
    ZoteroDepsMissingError,
    load_config,
    run,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the db2bibtex-sync CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Sync the Zotero library with the source database through the "
            "Zotero API: create missing records with the correct item type, "
            "update changed ones from the database, adopt matching "
            "Zotero-only items, and report the rest. Never deletes. "
            "Dry-run by default; pass --live to write."
        )
    )
    parser.add_argument(
        "--config",
        help="db_config.ini with [Zotero]/[CrossRef]/[Database]/[Driver] sections",
    )
    parser.add_argument("--library-id", help="Zotero library ID (overrides config)")
    parser.add_argument("--library-type", choices=["user", "group"], help="Zotero library type (overrides config)")
    parser.add_argument("--api-key", help="Zotero API key (overrides config)")
    parser.add_argument("--server", help="SQL Server hostname/instance (overrides config)")
    parser.add_argument("--database", help="Database name (overrides config)")
    parser.add_argument("--driver", help="ODBC driver name (overrides config)")
    parser.add_argument("--uid", help="SQL auth username (overrides config)")
    parser.add_argument("--pwd", help="SQL auth password (overrides config; prompted if --uid has none)")
    parser.add_argument("--trusted", action="store_true", help="Use Windows trusted connection")
    parser.add_argument(
        "--no-trust-server-certificate",
        action="store_true",
        help="Disable TrustServerCertificate=yes",
    )
    parser.add_argument(
        "--query-file",
        default=DEFAULT_QUERY_FILE,
        help=f"Export query (default {DEFAULT_QUERY_FILE}, the same file db2bibtex uses)",
    )
    parser.add_argument(
        "--before-year",
        type=int,
        default=DEFAULT_BEFORE_YEAR,
        help=f"Bound to the query's pub_year < ? parameter (default {DEFAULT_BEFORE_YEAR} = every year)",
    )
    parser.add_argument(
        "--collection",
        help="Collection key to put newly created items in (existing items keep their collections)",
    )
    parser.add_argument(
        "--no-fix-authors",
        action="store_true",
        help="Don't look up full author lists by DOI for 'et al.' records",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Write at most this many items (creates + updates + adopts) -- for a cautious first live run",
    )
    parser.add_argument("--live", action="store_true", help="Actually write to Zotero (default: dry run)")
    parser.add_argument(
        "--audit-path",
        default="zotero_sync_audit.xlsx",
        help="Audit workbook path (default zotero_sync_audit.xlsx)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser


def main(argv: "list | None" = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.verbose:
        # One line per API page otherwise -- hundreds of them on a full scan.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        file_cfg = load_config(args.config) if args.config else {}
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    library_id = args.library_id or file_cfg.get("library_id")
    library_type = args.library_type or file_cfg.get("library_type")
    api_key = args.api_key or file_cfg.get("api_key")
    server = args.server or file_cfg.get("server")
    database = args.database or file_cfg.get("database")
    uid = args.uid or file_cfg.get("uid")
    pwd = args.pwd or file_cfg.get("pwd")
    trust_cert = False if args.no_trust_server_certificate else file_cfg.get("trust_server_certificate", True)

    missing = [
        flag
        for flag, value in [
            ("--library-id", library_id),
            ("--library-type", library_type),
            ("--api-key", api_key),
            ("--server", server),
            ("--database", database),
        ]
        if not value
    ]
    if missing:
        print(
            f"Missing required settings: {', '.join(missing)} (pass directly, or use a --config file "
            "-- see db_config.ini.example).",
            file=sys.stderr,
        )
        return 2

    trusted = args.trusted or (uid is None)
    if not trusted and uid and not pwd:
        pwd = getpass.getpass(f"Password for {uid}@{server}: ")

    cfg = Config(
        library_id=library_id,
        library_type=library_type,
        api_key=api_key,
        server=server,
        database=database,
        driver=args.driver or file_cfg.get("driver") or DEFAULT_DRIVER,
        uid=uid,
        pwd=pwd,
        trusted_connection=trusted,
        trust_server_certificate=trust_cert,
        query_file=args.query_file,
        before_year=args.before_year,
        crossref_mailto=file_cfg.get("crossref_mailto"),
        fix_authors=not args.no_fix_authors,
        dry_run=not args.live,
        audit_path=args.audit_path,
        collection_key=args.collection,
        limit=args.limit,
    )

    try:
        plan = run(cfg, progress_callback=print)
    except (ZoteroDepsMissingError, PyodbcMissingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except QueryFileMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
        print(f"error: sync failed: {exc}", file=sys.stderr)
        return 1

    return 1 if plan.counts().get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
