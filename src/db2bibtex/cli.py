"""Command-line interface for db2bibtex."""

from __future__ import annotations

import argparse
import getpass
import sys

from db2bibtex.exporter import (
    DEFAULT_DRIVER,
    PyodbcMissingError,
    QueryFileMissingError,
    load_config,
    run_export,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the db2bibtex CLI."""
    parser = argparse.ArgumentParser(
        description=(
            "Export Published H.J. Andrews Experimental Forest / LTER "
            "publications from SQL Server to a BibTeX file for Zotero import."
        )
    )
    parser.add_argument(
        "--config",
        help="Path to an .ini config file with [Database]/[Driver] sections "
        "(recommended -- keeps password out of shell history)",
    )
    parser.add_argument("--server", help="SQL Server hostname/instance (overrides config)")
    parser.add_argument(
        "--database", help="Database name (overrides config). Required -- no default is assumed."
    )
    parser.add_argument(
        "--driver",
        help=f"ODBC driver name, e.g. '{DEFAULT_DRIVER}' (overrides config)",
    )
    parser.add_argument(
        "--before-year",
        type=int,
        default=1980,
        help="Export records with pub_year strictly less than this year "
        "(default 1980). E.g. --before-year 2026 exports everything "
        "through pub_year 2025.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output .bib path. Defaults to publications_before_<before-year>.bib",
    )
    parser.add_argument(
        "--uid",
        help="SQL auth username (overrides config; omit both for Windows/trusted auth)",
    )
    parser.add_argument(
        "--pwd",
        help="SQL auth password (overrides config). If --uid is given without "
        "--pwd, you'll be prompted securely.",
    )
    parser.add_argument(
        "--trusted",
        action="store_true",
        help="Use Windows trusted connection instead of SQL login",
    )
    parser.add_argument(
        "--no-trust-server-certificate",
        action="store_true",
        help="Disable TrustServerCertificate=yes (only if your cert chain is already trusted)",
    )
    parser.add_argument(
        "--query-file",
        help="Path to the SQL query file, e.g. query.sql after copying and "
        "adapting query.sql.example to your schema. Required -- no default "
        "is assumed.",
    )
    return parser


def main(argv: list | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = load_config(args.config) if args.config else {}

    server = args.server or cfg.get("server")
    database = args.database or cfg.get("database")
    driver = args.driver or cfg.get("driver") or DEFAULT_DRIVER
    uid = args.uid or cfg.get("uid")
    pwd = args.pwd or cfg.get("pwd")
    trust_cert = cfg.get("trust_server_certificate", True)
    if args.no_trust_server_certificate:
        trust_cert = False

    if not server:
        print("Missing --server (pass directly or via --config).", file=sys.stderr)
        return 2
    if not database:
        print("Missing --database (pass directly or via --config).", file=sys.stderr)
        return 2
    if not args.query_file:
        print(
            "Missing --query-file (e.g. --query-file query.sql, after copying "
            "and adapting query.sql.example).",
            file=sys.stderr,
        )
        return 2

    trusted = args.trusted or (uid is None)
    if not trusted and uid and not pwd:
        pwd = getpass.getpass(f"Password for {uid}@{server}: ")

    output_path = args.output or f"publications_before_{args.before_year}.bib"

    try:
        result = run_export(
            server=server,
            database=database,
            before_year=args.before_year,
            output_path=output_path,
            driver=driver,
            trusted_connection=trusted,
            uid=uid,
            pwd=pwd,
            trust_server_certificate=trust_cert,
            progress_callback=print,
            query_file=args.query_file,
        )
    except PyodbcMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except QueryFileMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
        print(f"error: export failed: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {result.entries_written} entries to {result.output_path}")
    if result.skipped_publication_ids:
        print(
            f"Skipped {len(result.skipped_publication_ids)} records "
            f"(missing title/author): {result.skipped_publication_ids}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
