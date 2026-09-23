# db2bibtex

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22800301.svg)](https://doi.org/10.5281/zenodo.22800301)

Export Published H.J. Andrews Experimental Forest / LTER publications from a
SQL Server database to a BibTeX (`.bib`) or RIS (`.ris`) file for import into
[Zotero](https://www.zotero.org/). BibTeX export ships as both a CLI and a
tkinter GUI; RIS export (`db2ris` -- see below) is CLI-only for now.

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
- PDF link resolved from `online_pdf` when populated, else derived from
  `pub_number` when the `pdf` flag column is set (same convention as the
  Andrews Forest Drupal publications page); written to a `pdf` field
  (Zotero auto-attaches it as a downloadable PDF)
- `url` field points at the publications detail page
  (`andrewsforest.oregonstate.edu/publications/<pub_number>`) whenever a
  PDF link was resolved, so the item's clickable URL opens the catalog
  record rather than a raw PDF; falls back to `online_linkage` only when
  no PDF link could be resolved
- `pub_number` also written to a `keywords` field so Zotero's BibTeX import
  turns it into a searchable tag (e.g. `pub_number:2135`)
- `reference_type`/`pub_type` mapped to a BibTeX entry type (article, book,
  incollection, inproceedings, phdthesis/mastersthesis, techreport,
  unpublished, or misc)
- BibTeX special characters (`& % $ # _ { } ~ ^`) escaped in free-text fields
- Records missing a title or author skipped and reported, not silently
  dropped

## db2ris -- an alternative to BibTeX

BibTeX has no `@newspaper` or `@magazine` entry type -- `@article` is the
only periodical type the format defines -- so the BibTeX export above has
to map Journal, Magazine, and Newspaper articles all to the same `article`
type, and Zotero's BibTeX-import translator then turns every one of those
into item type "Journal Article" on import, with no way to tell them apart
afterward. This was confirmed against Zotero's actual translator source
(`RIS.js`/`BibTeX.js`), not just the format specs.

RIS defines distinct type codes for all three (`JOUR`/`MGZN`/`NEWS`), and
Zotero's RIS-import translator imports them as distinct item types
(Journal/Magazine/Newspaper Article) -- also confirmed against the real
translator source. `db2ris` is a second exporter, in this same package,
that reads the exact same `query.sql` and `db_config.ini` as `db2bibtex`
and produces a `.ris` file instead. It also recovers a few other item
types BibTeX has no equivalent for at all (Audiovisual Material -> `film`,
Computer Program -> `computerProgram`, Map -> `map`), which fall back to
"misc" in the BibTeX export.

No extra setup: same config, same query file, same import flow (Zotero:
**File → Import...** → select the `.ris` file → **RIS**). The
`fix-authors`/`fix-item-types` tools above work unchanged on a
`db2ris`-imported library too -- they match a Zotero item back to its
source row via a child note's `publication_id` text, and that note gets
created the same way regardless of which exporter produced it.

`db2ris` is CLI-only for now (no GUI tab yet, mirroring how `db2bibtex`
itself started as CLI-only):

```bash
db2ris --config db_config.ini --query-file query.sql --before-year 2026 --output publications.ris
```

Same flags as `db2bibtex` throughout (`--server`, `--database`, `--driver`,
`--uid`/`--pwd`/`--trusted`, `--before-year`, `--query-file`) -- run
`db2ris --help` for the full list. If `reference_type` values differ from
what's guessed here, edit `RIS_TYPE_MAP` in `src/db2bibtex/ris_export.py`
the same way you'd edit `ENTRY_TYPE_MAP` for the BibTeX exporter.

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

The window has three tabs: **Export** (described above), **Compare**, and
**Fix Authors** (see below for both).

Fill in Server, Database, and Query file (or **File → Load Config...** for
Server/Database), check **Use Windows trusted connection** to skip
username/password, pick a before-year and output path, and click
**Run Export**. Server, Database, and Query file are all required -- none
of them default to any particular institution's setup. The export runs on a
background thread with a progress indicator and a scrolling log. **File →
Load Config...**/**Save Config...** cover all three tabs at once (Database
credentials and Zotero/CrossRef settings can live in the same `db_config.ini`
-- each tab's save only touches its own section, so saving from one tab never
erases another tab's settings). **Save Config...** warns before writing a
plaintext password and/or Zotero API key to disk.

## Zotero import notes

In Zotero: **File → Import...** → select the `.bib` file → **BibTeX**.
Zotero will:

- Auto-attach `pdf` field URLs (containing `://`) as downloadable PDFs
- Map the `keywords` field to tags, so `pub_number:2135` becomes a filterable
  tag in the tag selector
- Use the `AND<pub_number>` citation key group as-is (no cite-key collision
  handling needed on Zotero's side, since `db2bibtex` already de-duplicates)

## Comparing against your Zotero library

A fresh `db2bibtex` export from the database always contains every
in-scope publication, including ones you've already imported into Zotero
in a previous round. Re-importing the whole file risks creating duplicate
items. The **compare** feature reconciles a new database export against
your current Zotero library and produces a `.bib` file containing only the
entries you haven't imported yet.

First, export your current Zotero library to BibTeX: in Zotero, **File →
Export Library...** → format **BibTeX** → save it somewhere (e.g.
`zotero_library.bib`).

Matching is done by `publication_id`, extracted from each entry's `note`
field (`Source DB: publication_id X; pub_number Y; catalog_id Z`) — not by
citation key or the `keywords` tag. Citation key format and the
`keywords` tag were both added partway through this project's history, so
a real Zotero library can contain items imported under several different
historical exporter versions. The `note` field's `publication_id` is the
one identifier every exporter version has always written, making it the
only reliable join key across that mix.

CLI usage:

```bash
db2bibtex-compare --backup publications_before_2026.bib --library zotero_library.bib --output missing.bib
```

`--output` defaults to `missing_from_library.bib` if omitted. The command
prints how many entries were found in each file and how many are missing,
and warns (to stderr) about any backup entries it couldn't match to a
`publication_id`.

The GUI's **Compare** tab offers the same fields (Backup file, Library
export file, Output file) and a **Run Comparison** button, running the
comparison on a background thread with the same log/error handling as the
Export tab.

Either way, `missing.bib` still needs to be imported into Zotero yourself
(**File → Import...**) — this tool does not write to Zotero directly.

## Fixing truncated "et al." authors

Some Zotero items end up with a creator list truncated to a literal
"et al." entry instead of the real authors (e.g. from an import that capped
the author count). The **fix-authors** feature scans a Zotero library for
items with that sentinel, looks up the full author list from the item's DOI
(CrossRef first, DataCite as a fallback), and replaces the creator list in
place -- every other field (tags, collections, notes, Extra, attachments,
relations, version) is left untouched. This feature writes directly to your
live Zotero library via its API and is completely independent of the
export/compare features above -- it doesn't touch the SQL Server database or
either `.bib` workflow.

It needs a Zotero API key (**Settings → Feeds/API** in Zotero) and your
library ID, plus an email address for CrossRef's ["polite pool"](https://api.crossref.org/swagger-ui/index.html)
(faster, more reliable lookups). Add these to `db_config.ini` (same file as
the database config, see `db_config.ini.example`):

```ini
[Zotero]
library_id = your_zotero_library_id_here
library_type = group_or_user
api_key = your_zotero_api_key_here

[CrossRef]
mailto = your_email@example.org
```

**Dry-run by default** -- no changes are written to Zotero unless you pass
`--live` (CLI) or check **Apply changes live** (GUI). Every item scanned is
written to an audit workbook (`item_key, title, doi, old_creators,
new_creators, status, detail`, as an `.xlsx` file -- native Unicode, no
CSV encoding/delimiter ambiguity) whether or not it changed, so review
that file before ever running live. **Test against one collection first**
(`--collection`
CLI flag / **Collection** GUI field) before running across the whole
library -- the GUI warns you if you check **Apply changes live** with no
collection set.

CLI usage:

```bash
db2bibtex-fix-authors --config db_config.ini --collection ABCD1234
db2bibtex-fix-authors --config db_config.ini --collection ABCD1234 --live
db2bibtex-fix-authors --config db_config.ini --live   # whole library, once you trust the results
```

Run `db2bibtex-fix-authors --help` for the full flag list. If pyzotero/
`requests`/`openpyxl` aren't installed, install the extra:
`pip install -e ".[zotero]"`.

The GUI's **Fix Authors** tab offers the same fields (Library ID, Library
type, API key, CrossRef mailto, optional Collection, Audit output path) plus
an **Apply changes live** checkbox (unchecked = dry-run) and a
**Scan & Fix Authors** button, running on a background thread with the same
log/error handling as the other tabs.

## Fixing misclassified Newspaper/Magazine articles

BibTeX has no `@newspaper` or `@magazine` entry type — `@article` is the
only periodical type the format defines. `db2bibtex`'s exporter therefore
maps `Journal Article`, `Magazine Article`, and `Newspaper Article`
`reference_type` values all to BibTeX's `article` type, and Zotero's
BibTeX-import translator unconditionally turns every `@article` into item
type "Journal Article" on import, regardless of what's actually in the
journal/publisher field (e.g. a Seattle Times piece imports as "Journal
Article" with journal = "The Seattle Times"). This is a real limitation of
the BibTeX round trip, not a bug in the `reference_type` mapping.

The **fix-item-types** feature scans an already-imported Zotero library for
"Journal Article" items, looks up each one's real `reference_type` back in
the source database (matched via the `publication_id` embedded in the
child note Zotero's BibTeX import creates from the exporter's `note`
field — *not* the `keywords` tag, for the same mixed-vintage-library
reason described above), and converts just the ones that are really
Newspaper or Magazine articles to the correct Zotero item type in place.
Every field the new type still supports is carried over unchanged; it also
backfills the newspaper `section` field from the database's `news_section`
column, which db2bibtex's BibTeX export doesn't currently carry through at
all. Like fix-authors, this writes directly to your live Zotero library via
its API and doesn't touch either `.bib` workflow.

It needs both the `[Zotero]` settings (same as fix-authors, above) and the
`[Database]`/`[Driver]` settings (same as the main export, see
Configuration) in the same `db_config.ini`. It also needs its own lookup
query file: copy `query_item_type_lookup.sql.example` to
`query_item_type_lookup.sql` and adapt the table/column names to your
schema (same convention as `query.sql`; gitignored, never committed).

**Dry-run by default** — no changes are written to Zotero unless you pass
`--live`. Every item checked is written to an audit workbook (`item_key,
title, old_item_type, new_item_type, publication_id, status, detail`)
whether or not it changed, so review that file before ever running live.

**Test against one collection first** (`--collection`) before running
across the whole library — this isn't just a safety precaution here: the
tool has to scan *every* note in scope (library-wide, or just the
collection) to find each item's `publication_id`, since that identifier
lives in a child note rather than anywhere searchable/filterable via the
Zotero API. On a large library this full-library note scan can take
several minutes (tens of thousands of notes at ~100/page); scoping to a
collection makes it near-instant.

CLI usage:

```bash
db2bibtex-fix-item-types --config db_config.ini --collection ABCD1234
db2bibtex-fix-item-types --config db_config.ini --collection ABCD1234 --live
db2bibtex-fix-item-types --config db_config.ini --live   # whole library, once you trust the results
```

Run `db2bibtex-fix-item-types --help` for the full flag list.

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
