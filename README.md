# db2bibtex

<!-- [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.XXXXXXX.svg)](https://doi.org/10.5281/zenodo.XXXXXXX) -->

Export Published H.J. Andrews Experimental Forest / LTER publications from a
SQL Server database to a BibTeX (`.bib`) file for import into
[Zotero](https://www.zotero.org/). Ships as both a CLI and a tkinter GUI.

## What it does

Connects to your SQL Server publications database and runs the query
defined in `query.sql` (see `query.sql.example` for the expected shape and
`Configuration` below) — typically a filter down to published, non-abstract
records with a `pub_year` before a cutoff you choose — then writes a `.bib`
file with:

- Multi-author fields (delimited `//` in the source data) converted to
  proper `and`-joined BibTeX author lists
- Citation keys following the production Andrews Forest convention
  (`AND<pub_number>`, e.g. `AND2135`)
- `online_pdf` written to a `pdf` field (Zotero auto-attaches it as a
  downloadable PDF) and `online_linkage` written to a separate `url` field
- `pub_number` also written to a `keywords` field so Zotero's BibTeX import
  turns it into a searchable tag (e.g. `pub_number:2135`)
- `reference_type`/`pub_type` mapped to a BibTeX entry type (article, book,
  incollection, inproceedings, phdthesis/mastersthesis, techreport,
  unpublished, or misc)
- BibTeX special characters (`& % $ # _ { } ~ ^`) escaped in free-text fields
- Records missing a title or author skipped and reported, not silently
  dropped

## Installation

```bash
git clone https://github.com/andrewsforestlter/db2bibtex.git
cd db2bibtex
python3 -m venv venv
source venv/bin/activate      # or venv\Scripts\activate on Windows
pip install -e .
```

This installs an ODBC driver dependency (`pyodbc`), but you also need the
**ODBC Driver 18 for SQL Server** and the unixODBC system library installed
separately (see Microsoft's driver installation docs for your OS).

## Configuration

Copy the example config and fill in real values:

```bash
cp db_config.ini.example db_config.ini
```

```ini
[Database]
server = your.server.name.here
database = your_database_here
username = your_username_here
password = your_password_here
trustservercertificate = yes/no

[Driver]
driver = {your sql driver details go here}
```

`db_config.ini` is gitignored — never commit it. CLI/GUI values always
override the config file, and Windows trusted-connection auth is available
as an alternative to a username/password.

The SQL query itself follows the same pattern: copy `query.sql.example` to
`query.sql` and adapt the table/column names and filter values to your own
database schema.

```bash
cp query.sql.example query.sql
```

`query.sql` is also gitignored — it's expected to contain your real,
institution-specific schema details, which is why it's never committed.
Override its location with `--query-file` (CLI) or the **Query file** field
(GUI) if you don't want it at the project root.

## CLI usage

```bash
db2bibtex --config db_config.ini --query-file query.sql --before-year 1980
db2bibtex --config db_config.ini --query-file query.sql --before-year 2026 --output recent.bib
db2bibtex --server your.server.name.here --database your_database_here --trusted --query-file query.sql --before-year 1980
```

`--database` and `--query-file` are required (`--database` can come from
`--config` instead; `--query-file` has no config-file equivalent and must
always be passed explicitly) -- same as `--server`, nothing here defaults
to any particular institution's setup.

Run `db2bibtex --help` for the full flag list (`--server`, `--database`,
`--driver`, `--query-file`, `--before-year`, `--output`, `--uid`, `--pwd`,
`--trusted`, `--no-trust-server-certificate`). If `--uid` is given without
`--pwd`, you're prompted securely via `getpass`.

## GUI usage

```bash
db2bibtex-gui
# or
python -m db2bibtex
```

Fill in Server, Database, and Query file (or **File → Load Config...** for
Server/Database), check **Use Windows trusted connection** to skip
username/password, pick a before-year and output path, and click
**Run Export**. Server, Database, and Query file are all required -- none
of them default to any particular institution's setup. The export runs on a
background thread with a progress indicator and a scrolling log. **File →
Save Config...** warns before writing a plaintext password to disk.

## Zotero import notes

In Zotero: **File → Import...** → select the `.bib` file → **BibTeX**.
Zotero will:

- Auto-attach `pdf` field URLs (containing `://`) as downloadable PDFs
- Map the `keywords` field to tags, so `pub_number:2135` becomes a filterable
  tag in the tag selector
- Use the `AND<pub_number>` citation key group as-is (no cite-key collision
  handling needed on Zotero's side, since `db2bibtex` already de-duplicates)

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The test suite passes even when `pyodbc` is installed but its `import` fails
at runtime (e.g. a CI runner missing the unixODBC system library) — database
connection code is only exercised through mocks.

---

This material is based upon work supported by the H.J. Andrews Experimental
Forest and Long Term Ecological Research (LTER) program under the NSF grant
LTER8 DEB-2025755.
