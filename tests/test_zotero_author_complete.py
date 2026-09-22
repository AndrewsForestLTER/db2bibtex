"""Tests for db2bibtex.zotero_author_complete: 'et al.' detection, DOI
lookup, and the full-replace creator update pipeline.

No real Zotero/CrossRef/DataCite calls are made -- pyzotero and requests
are mocked throughout, same convention as test_exporter.py's pyodbc mocks.
"""

from __future__ import annotations

import configparser
from unittest.mock import MagicMock

import pytest

from db2bibtex import zotero_author_complete as zac


# ---------------------------------------------------------------------------
# is_incomplete
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "last_name",
    ["et al", "et al.", "Et Al.", "ET AL", "et. al.", "et.al.", "  et al.  "],
)
def test_is_incomplete_detects_et_al_variants_two_field(last_name):
    creators = [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Smith"},
        {"creatorType": "author", "firstName": "", "lastName": last_name},
    ]
    assert zac.is_incomplete(creators) is True


def test_is_incomplete_detects_et_al_single_field_name():
    creators = [{"creatorType": "author", "name": "et al."}]
    assert zac.is_incomplete(creators) is True


def test_is_incomplete_detects_et_al_split_across_first_and_last_name():
    """Real-world shape found in a live library sample: Zotero's two-field
    creator editor held firstName='et', lastName='al' (displayed as
    'al, et') rather than the sentinel landing in one field."""
    creators = [
        {"creatorType": "author", "firstName": "N. L.", "lastName": "Stephenson"},
        {"creatorType": "author", "firstName": "et", "lastName": "al"},
    ]
    assert zac.is_incomplete(creators) is True


def test_is_incomplete_does_not_false_positive_on_real_first_last_names():
    # A normal two-field author must not be caught by the combined check.
    creators = [{"creatorType": "author", "firstName": "N. L.", "lastName": "Stephenson"}]
    assert zac.is_incomplete(creators) is False


def test_is_incomplete_detects_et_al_suffix_on_real_last_author():
    """Real-world shape found in a second live library sample: no separate
    'et al.' creator at all -- the sentinel was appended directly onto the
    last real author's given-name field ('Brown, Cynthia S. et al')."""
    creators = [
        {"creatorType": "author", "firstName": "Elizabeth T.", "lastName": "Borer"},
        {"creatorType": "author", "firstName": "Eric W.", "lastName": "Seabloom"},
        {"creatorType": "author", "firstName": "Cynthia S. et al", "lastName": "Brown"},
    ]
    assert zac.is_incomplete(creators) is True


@pytest.mark.parametrize(
    "raw_field",
    [
        # "//"-delimited source data, sentinel as its own trailing segment
        # (BibTeX/Zotero parses the bare two-word "et al." into first/last).
        "Miralha, L.//et al.",
        # No trailing period.
        "Courbet, F.//Curt, T.//et al",
        # "//" immediately touching "et al." with no space -- only
        # plausible if the raw "//" ever reaches Zotero unconverted.
        "Gremel,S//et al.",
        # ";"-delimited source rows: the db2bibtex exporter only recognizes
        # "//", so this entire blob likely lands in one Zotero field.
        "Hollen, B.; Kendall, W.; Levi, T.; et al.",
        # "et al." glued directly onto the last real name, no delimiter at
        # all in the source data.
        "Collins, Scott L. et al",
        "Tsoumakas, Grigorios et al.",
    ],
)
def test_is_incomplete_catches_real_source_database_variants_as_name_field(raw_field):
    """Regression test built directly from a real sample of the source
    database's `//`-delimited author field (see exporter.py's docs on that
    format) -- whichever delimiter style a given row used, or didn't use,
    is exactly where 'et al.' ends up landing once it reaches Zotero."""
    creators = [{"creatorType": "author", "name": raw_field}]
    assert zac.is_incomplete(creators) is True


def test_is_incomplete_catches_semicolon_delimited_blob_split_on_first_comma():
    """BibTeX/Zotero's 'Last, First' parsing splits on the first comma, so
    an unrecognized ';'-delimited source row ('Hollen, B.; Kendall, W.;
    Levi, T.; et al.') lands as one creator with everything after that
    first comma -- 'et al.' and all -- crammed into firstName."""
    creators = [
        {
            "creatorType": "author",
            "firstName": "B.; Kendall, W.; Levi, T.; et al.",
            "lastName": "Hollen",
        }
    ]
    assert zac.is_incomplete(creators) is True


def test_is_incomplete_does_not_false_positive_on_surnames_ending_in_al():
    # "Alvarez"/"Albert"/"Beckman"-style surnames must not trip the
    # end-anchored suffix match just because they end near "al".
    creators = [
        {"creatorType": "author", "firstName": "Robert", "lastName": "Alvarez"},
        {"creatorType": "author", "firstName": "N. G.", "lastName": "Beckman"},
    ]
    assert zac.is_incomplete(creators) is False


def test_is_incomplete_false_for_normal_creator_list():
    creators = [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Smith"},
        {"creatorType": "author", "firstName": "John", "lastName": "Doe"},
    ]
    assert zac.is_incomplete(creators) is False


def test_is_incomplete_does_not_false_positive_on_similar_names():
    # A real (if rare) surname "Etal" must not be mistaken for the sentinel
    # -- a false positive here would silently overwrite a real author.
    creators = [{"creatorType": "author", "firstName": "A.", "lastName": "Etal"}]
    assert zac.is_incomplete(creators) is False


def test_is_incomplete_requires_separator_between_et_and_al():
    # "etal" with no period/space is deliberately not treated as the
    # sentinel, for the same false-positive reason as the test above.
    creators = [{"creatorType": "author", "lastName": "etal"}]
    assert zac.is_incomplete(creators) is False


def test_is_incomplete_empty_list():
    assert zac.is_incomplete([]) is False


# ---------------------------------------------------------------------------
# get_doi
# ---------------------------------------------------------------------------

def test_get_doi_present():
    assert zac.get_doi({"DOI": " 10.1234/abcd "}) == "10.1234/abcd"


def test_get_doi_missing():
    assert zac.get_doi({}) is None


def test_get_doi_blank():
    assert zac.get_doi({"DOI": "   "}) is None


# ---------------------------------------------------------------------------
# to_zotero_creators
# ---------------------------------------------------------------------------

def test_to_zotero_creators_maps_given_family():
    authors = [{"given": "Jane", "family": "Smith"}, {"given": "John", "family": "Doe"}]
    assert zac.to_zotero_creators(authors) == [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Smith"},
        {"creatorType": "author", "firstName": "John", "lastName": "Doe"},
    ]


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        zac.load_config(tmp_path / "does_not_exist.ini")


def test_load_config_matches_example_format(tmp_path):
    """Mirrors the [Zotero]/[CrossRef] sections added to db_config.ini.example."""
    config_path = tmp_path / "db_config.ini"
    cp = configparser.ConfigParser()
    cp["Zotero"] = {
        "library_id": "12345",
        "library_type": "group",
        "api_key": "secret-key",
    }
    cp["CrossRef"] = {"mailto": "someone@example.org"}
    with open(config_path, "w") as f:
        cp.write(f)

    result = zac.load_config(config_path)
    assert result == {
        "library_id": "12345",
        "library_type": "group",
        "api_key": "secret-key",
        "crossref_mailto": "someone@example.org",
    }


def test_save_config_round_trip(tmp_path):
    config_path = tmp_path / "db_config.ini"
    zac.save_config(
        config_path,
        library_id="12345",
        library_type="group",
        api_key="secret-key",
        crossref_mailto="someone@example.org",
    )

    result = zac.load_config(config_path)
    assert result == {
        "library_id": "12345",
        "library_type": "group",
        "api_key": "secret-key",
        "crossref_mailto": "someone@example.org",
    }


def test_save_config_preserves_other_sections(tmp_path):
    """save_config() must not clobber unrelated sections (e.g. [Database]/
    [Driver], written by exporter.save_config) already in the same shared
    db_config.ini file."""
    config_path = tmp_path / "db_config.ini"
    cp = configparser.ConfigParser()
    cp["Database"] = {"server": "testserver", "database": "testdb"}
    with open(config_path, "w") as f:
        cp.write(f)

    zac.save_config(
        config_path,
        library_id="12345",
        library_type="group",
        api_key="secret-key",
        crossref_mailto="someone@example.org",
    )

    result = configparser.ConfigParser()
    result.read(config_path)
    assert result["Database"]["server"] == "testserver"
    assert result["Zotero"]["library_id"] == "12345"


# ---------------------------------------------------------------------------
# Missing optional dependencies
# ---------------------------------------------------------------------------

def test_get_client_raises_when_pyzotero_missing(monkeypatch):
    monkeypatch.setattr(zac, "zotero", None)
    cfg = zac.Config(library_id="1", library_type="group", api_key="k", crossref_mailto="m@x.org")
    with pytest.raises(zac.ZoteroDepsMissingError):
        zac.get_client(cfg)


def test_fetch_authors_crossref_raises_when_requests_missing(monkeypatch):
    monkeypatch.setattr(zac, "requests", None)
    with pytest.raises(zac.ZoteroDepsMissingError):
        zac.fetch_authors_crossref("10.1234/abcd", "m@x.org")


# ---------------------------------------------------------------------------
# fetch_authors_crossref / fetch_authors_datacite (requests mocked)
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


def test_fetch_authors_crossref_success(monkeypatch):
    payload = {"message": {"author": [{"given": "Jane", "family": "Smith"}, {"given": "John", "family": "Doe"}]}}
    mock_get = MagicMock(return_value=_mock_response(200, payload))
    monkeypatch.setattr(zac.requests, "get", mock_get)

    result = zac.fetch_authors_crossref("10.1234/abcd", "me@example.org")

    assert result == [{"given": "Jane", "family": "Smith"}, {"given": "John", "family": "Doe"}]
    # polite pool: mailto goes in the User-Agent header, not query params
    _, kwargs = mock_get.call_args
    assert "me@example.org" in kwargs["headers"]["User-Agent"]


def test_fetch_authors_crossref_not_found(monkeypatch):
    monkeypatch.setattr(zac.requests, "get", MagicMock(return_value=_mock_response(404)))
    assert zac.fetch_authors_crossref("10.1234/nope", "me@example.org") is None


def test_fetch_authors_crossref_no_authors(monkeypatch):
    monkeypatch.setattr(
        zac.requests, "get", MagicMock(return_value=_mock_response(200, {"message": {}}))
    )
    assert zac.fetch_authors_crossref("10.1234/abcd", "me@example.org") is None


def test_fetch_authors_datacite_success_comma_name(monkeypatch):
    payload = {"data": {"attributes": {"creators": [{"name": "Smith, Jane"}, {"name": "Doe, John"}]}}}
    monkeypatch.setattr(zac.requests, "get", MagicMock(return_value=_mock_response(200, payload)))

    result = zac.fetch_authors_datacite("10.5555/xyz")

    assert result == [{"given": "Jane", "family": "Smith"}, {"given": "John", "family": "Doe"}]


def test_fetch_authors_datacite_success_single_name(monkeypatch):
    payload = {"data": {"attributes": {"creators": [{"name": "SomeOrg"}]}}}
    monkeypatch.setattr(zac.requests, "get", MagicMock(return_value=_mock_response(200, payload)))

    result = zac.fetch_authors_datacite("10.5555/xyz")

    assert result == [{"given": "", "family": "SomeOrg"}]


def test_fetch_authors_datacite_not_found(monkeypatch):
    monkeypatch.setattr(zac.requests, "get", MagicMock(return_value=_mock_response(404)))
    assert zac.fetch_authors_datacite("10.5555/nope") is None


# ---------------------------------------------------------------------------
# process_item / find_incomplete_items (pyzotero client mocked)
# ---------------------------------------------------------------------------

def _item(key, title, doi, creators):
    return {"data": {"key": key, "title": title, "DOI": doi, "creators": creators}}


def _cfg(**overrides):
    base = dict(
        library_id="1",
        library_type="group",
        api_key="k",
        crossref_mailto="me@example.org",
        dry_run=True,
        crossref_rate_limit_sec=0,  # keep tests fast
    )
    base.update(overrides)
    return zac.Config(**base)


def test_process_item_skips_no_doi():
    item = _item("ABC1", "Some Title", "", [{"lastName": "et al."}])
    row = zac.process_item(zot=MagicMock(), item=item, cfg=_cfg())
    assert row.status == "skipped-no-doi"
    assert row.item_key == "ABC1"


def test_process_item_skips_lookup_failed(monkeypatch):
    monkeypatch.setattr(zac, "fetch_authors_crossref", lambda doi, mailto: None)
    monkeypatch.setattr(zac, "fetch_authors_datacite", lambda doi: None)
    item = _item("ABC2", "Some Title", "10.1234/abcd", [{"lastName": "et al."}])

    row = zac.process_item(zot=MagicMock(), item=item, cfg=_cfg())

    assert row.status == "skipped-lookup-failed"
    assert row.doi == "10.1234/abcd"


def test_process_item_old_creators_reveals_suffix_hidden_in_first_name(monkeypatch):
    """The audit CSV's old_creators column is the reviewer's only way to
    verify *why* an item was flagged before trusting --live -- it must
    show firstName, not just lastName, or a suffix-only match ("Cynthia
    S. et al"/lastName "Brown") shows up as a clean-looking name list
    with no visible evidence for the flag."""
    monkeypatch.setattr(
        zac, "fetch_authors_crossref", lambda doi, mailto: [{"given": "Jane", "family": "Smith"}]
    )
    item = _item(
        "ABCX",
        "Some Title",
        "10.1234/abcd",
        [
            {"firstName": "Elizabeth T.", "lastName": "Borer"},
            {"firstName": "Cynthia S. et al", "lastName": "Brown"},
        ],
    )

    row = zac.process_item(zot=MagicMock(), item=item, cfg=_cfg())

    assert row.old_creators == "Elizabeth T. Borer; Cynthia S. et al Brown"


def test_process_item_dry_run_does_not_call_update(monkeypatch):
    monkeypatch.setattr(
        zac, "fetch_authors_crossref", lambda doi, mailto: [{"given": "Jane", "family": "Smith"}]
    )
    zot = MagicMock()
    item = _item("ABC3", "Some Title", "10.1234/abcd", [{"lastName": "Old"}, {"lastName": "et al."}])

    row = zac.process_item(zot=zot, item=item, cfg=_cfg(dry_run=True))

    assert row.status == "dry-run"
    assert row.new_creators == "Smith"
    zot.update_item.assert_not_called()
    zot.item.assert_not_called()


def test_process_item_live_full_replace_preserves_other_fields(monkeypatch):
    monkeypatch.setattr(
        zac, "fetch_authors_crossref", lambda doi, mailto: [{"given": "Jane", "family": "Smith"}]
    )
    fresh = {
        "data": {
            "key": "ABC4",
            "title": "Some Title",
            "DOI": "10.1234/abcd",
            "creators": [{"lastName": "Old"}, {"lastName": "et al."}],
            "tags": [{"tag": "keep-me"}],
            "extra": "keep this too",
            "version": 42,
        }
    }
    zot = MagicMock()
    zot.item.return_value = fresh
    item = _item("ABC4", "Some Title", "10.1234/abcd", [{"lastName": "Old"}, {"lastName": "et al."}])

    row = zac.process_item(zot=zot, item=item, cfg=_cfg(dry_run=False))

    assert row.status == "updated"
    zot.item.assert_called_once_with("ABC4")
    updated_item = zot.update_item.call_args[0][0]
    assert updated_item["data"]["creators"] == [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Smith"}
    ]
    # every other field on the freshly re-fetched item must survive untouched
    assert updated_item["data"]["tags"] == [{"tag": "keep-me"}]
    assert updated_item["data"]["extra"] == "keep this too"
    assert updated_item["data"]["version"] == 42


def test_process_item_live_update_error_is_captured_not_raised(monkeypatch):
    monkeypatch.setattr(
        zac, "fetch_authors_crossref", lambda doi, mailto: [{"given": "Jane", "family": "Smith"}]
    )
    zot = MagicMock()
    zot.item.return_value = {"data": {"key": "ABC5", "creators": []}}
    zot.update_item.side_effect = RuntimeError("version conflict")
    item = _item("ABC5", "Some Title", "10.1234/abcd", [{"lastName": "et al."}])

    row = zac.process_item(zot=zot, item=item, cfg=_cfg(dry_run=False))

    assert row.status == "error"
    assert "version conflict" in row.detail


def test_find_incomplete_items_filters_and_paginates_top():
    zot = MagicMock()
    incomplete = _item("A", "t1", "10.1/a", [{"lastName": "et al."}])
    complete = _item("B", "t2", "10.1/b", [{"lastName": "Smith"}])
    zot.everything.return_value = [incomplete, complete]

    result = zac.find_incomplete_items(zot, collection_key=None)

    assert result == [incomplete]
    zot.top.assert_called_once()
    zot.collection_items.assert_not_called()


def test_find_incomplete_items_uses_collection_when_given():
    zot = MagicMock()
    zot.everything.return_value = []

    zac.find_incomplete_items(zot, collection_key="COLLKEY")

    zot.collection_items.assert_called_once_with("COLLKEY")
    zot.top.assert_not_called()


# ---------------------------------------------------------------------------
# write_audit_xlsx
# ---------------------------------------------------------------------------

def test_write_audit_xlsx_raises_when_openpyxl_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(zac, "Workbook", None)
    with pytest.raises(zac.ZoteroDepsMissingError):
        zac.write_audit_xlsx([], str(tmp_path / "audit.xlsx"))


def test_write_audit_xlsx(tmp_path):
    rows = [
        zac.AuditRow("A", "Title A", "10.1/a", "Old", "New", "updated"),
        zac.AuditRow("B", "Title B", "", "Old", "", "skipped-no-doi"),
    ]
    out = tmp_path / "audit.xlsx"

    zac.write_audit_xlsx(rows, str(out))

    from openpyxl import load_workbook

    ws = load_workbook(out).active
    header = [c.value for c in ws[1]]
    assert header == zac.AUDIT_HEADERS
    assert [c.value for c in ws[2]][:3] == ["A", "Title A", "10.1/a"]
    assert ws[2][5].value == "updated"
    assert ws[3][5].value == "skipped-no-doi"


def test_write_audit_xlsx_preserves_unicode_natively(tmp_path):
    """A CSV forced fighting encoding/delimiter ambiguity for no benefit --
    a real workbook stores Unicode natively, no codepage guessing that
    used to mangle accented author names (e.g. 'Antão' -> 'AntÃ£o')."""
    rows = [zac.AuditRow("A", "Title", "10.1/a", "Antão; Błażewicz", "New", "updated")]
    out = tmp_path / "audit.xlsx"

    zac.write_audit_xlsx(rows, str(out))

    from openpyxl import load_workbook

    ws = load_workbook(out).active
    assert ws[2][3].value == "Antão; Błażewicz"


# ---------------------------------------------------------------------------
# run() orchestration
# ---------------------------------------------------------------------------

def test_run_raises_when_pyzotero_missing(monkeypatch):
    monkeypatch.setattr(zac, "zotero", None)
    cfg = _cfg(audit_path="unused.xlsx")
    with pytest.raises(zac.ZoteroDepsMissingError):
        zac.run(cfg, progress_callback=lambda *_: None)


def test_run_writes_audit_xlsx_and_counts(monkeypatch, tmp_path):
    fake_zotero_module = MagicMock()
    fake_client = MagicMock()
    fake_client.everything.return_value = [
        _item("A", "t1", "10.1/a", [{"lastName": "et al."}]),
        _item("B", "t2", "10.1/b", [{"lastName": "Smith"}]),  # not flagged
    ]
    fake_zotero_module.Zotero.return_value = fake_client
    monkeypatch.setattr(zac, "zotero", fake_zotero_module)
    monkeypatch.setattr(zac, "fetch_authors_crossref", lambda doi, mailto: None)
    monkeypatch.setattr(zac, "fetch_authors_datacite", lambda doi: None)

    audit_path = tmp_path / "audit.xlsx"
    cfg = _cfg(audit_path=str(audit_path))
    messages = []

    result = zac.run(cfg, progress_callback=messages.append)

    assert result.counts == {"skipped-lookup-failed": 1}
    assert audit_path.exists()
    assert any("Found 1 items" in m for m in messages)
