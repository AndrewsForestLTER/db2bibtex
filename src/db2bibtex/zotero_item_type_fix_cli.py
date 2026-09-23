"""Command-line interface for db2bibtex's fix-item-types feature.

Scans a Zotero library for "Journal Article" items that are really
Newspaper or Magazine articles per the source database's reference_type
(a limitation of BibTeX, which has no @newspaper/@magazine entry type --
see zotero_item_type_fix.py's module docstring), and converts just those
items' Zotero item type in place. Runs independently of the export/compare/
fix-authors features -- no shared state, config, or sequencing dependency
with them, though it reads the same db_config.ini.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from db2bibtex.exporter import DEFAULT_DRIVER, PyodbcMissingError, QueryFileMissingError
from db2bibtex.zotero_item_type_fix import (
    DEFAULT_QUERY_FILE,
    Config,
    ZoteroDepsMissingError,
    load_config,
    run,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the db2bibtex-fix-item-types CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Scan a Zotero library for 'Journal Article' items that are "
            "really Newspaper or Magazine articles per the source "
            "database's reference_type, and convert just those items' "
            "Zotero item type in place. Dry-run by default; pass --live "
            "to actually write changes."
        )
    )
    parser.add_argument(
        "--config",
        help="Path to an .ini config file with [Zotero]/[Database]/[Driver] "
        "sections (recommended -- keeps credentials out of shell history; "
        "see db_config.ini.example)",
    )
    parser.add_argument("--library-id", help="Zotero library ID (overrides config)")
    parser.add_argument(
        "--library-type", choices=["user", "group"], help="Zotero library type (overrides config)"
    )
    parser.add_argument("--api-key", help="Zotero API key (overrides config)")
    parser.add_argument("--server", help="SQL Server hostname/instance (overrides config)")
    parser.add_argument("--database", help="Database name (overrides config)")
    parser.add_argument("--driver", help="ODBC driver name (overrides config)")
    parser.add_argument(
        "--uid", help="SQL auth username (overrides config; omit both for Windows/trusted auth)"
    )
    parser.add_argument(
        "--pwd",
        help="SQL auth password (overrides config). If --uid is given without "
        "--pwd, you'll be prompted securely.",
    )
    parser.add_argument(
        "--trusted", action="store_true", help="Use Windows trusted connection instead of SQL login"
    )
    parser.add_argument(
        "--no-trust-server-certificate",
        action="store_true",
        help="Disable TrustServerCertificate=yes (only if your cert chain is already trusted)",
    )
    parser.add_argument(
        "--query-file",
        default=DEFAULT_QUERY_FILE,
        help=f"Path to the lookup query file (default {DEFAULT_QUERY_FILE}, after "
        "copying and adapting query_item_type_lookup.sql.example to your schema)",
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
        default="zotero_item_type_fix_audit.xlsx",
        help="Output .xlsx audit workbook path (default zotero_item_type_fix_audit.xlsx)",
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
    server = args.server or file_cfg.get("server")
    database = args.database or file_cfg.get("database")
    driver = args.driver or file_cfg.get("driver")
    uid = args.uid or file_cfg.get("uid")
    pwd = args.pwd or file_cfg.get("pwd")
    trust_cert = file_cfg.get("trust_server_certificate", True)
    if args.no_trust_server_certificate:
        trust_cert = False

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
            f"Missing required settings: {', '.join(missing)} "
            f"(pass directly, or add [Zotero]/[Database] sections to a "
            f"--config file -- see db_config.ini.example).",
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
        driver=driver or DEFAULT_DRIVER,
        uid=uid,
        pwd=pwd,
        trusted_connection=trusted,
        trust_server_certificate=trust_cert,
        query_file=args.query_file,
        dry_run=not args.live,
        audit_path=args.audit_path,
        collection_key=args.collection,
    )

    try:
        result = run(cfg, progress_callback=print)
    except ZoteroDepsMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except PyodbcMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except QueryFileMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
        print(f"error: fix-item-types run failed: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote audit log ({len(result.rows)} rows) to {cfg.audit_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
