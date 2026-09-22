"""GUI interaction tests for db2bibtex.gui.

Instantiates the real tkinter window against a real display (requires
$DISPLAY, e.g. WSLg or Xvfb) and exercises interactive logic directly.
messagebox popups are mocked so nothing blocks waiting for a click.
"""

from __future__ import annotations

import os
import tkinter as tk
from unittest.mock import MagicMock, patch

import pytest

from db2bibtex import gui as gui_module
from db2bibtex.exporter import ExportResult, PyodbcMissingError, QueryFileMissingError
from db2bibtex.zotero_author_complete import AuditRow, RunResult, ZoteroDepsMissingError

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"),
    reason="requires a display (DISPLAY/WAYLAND_DISPLAY not set)",
)


@pytest.fixture
def app():
    application = gui_module.App()
    application.withdraw()  # don't actually show the window
    yield application
    application.destroy()


def test_trusted_toggle_disables_credentials(app):
    app.trusted_var.set(True)
    app.on_trusted_toggle()
    assert str(app.username_entry.cget("state")) == "disabled"
    assert str(app.password_entry.cget("state")) == "disabled"

    app.trusted_var.set(False)
    app.on_trusted_toggle()
    assert str(app.username_entry.cget("state")) == "normal"
    assert str(app.password_entry.cget("state")) == "normal"


def test_password_visibility_toggle(app):
    assert app.password_entry.cget("show") == "*"
    app.show_password_var.set(True)
    app.on_show_password_toggle()
    assert app.password_entry.cget("show") == ""
    app.show_password_var.set(False)
    app.on_show_password_toggle()
    assert app.password_entry.cget("show") == "*"


def test_config_load_save_round_trip(app, tmp_path):
    config_path = tmp_path / "roundtrip.ini"

    app.server_var.set("testserver.example.edu")
    app.database_var.set("testdb")
    app.driver_var.set("ODBC Driver 18 for SQL Server")
    app.trusted_var.set(False)
    app.username_var.set("myuser")
    app.password_var.set("mypassword")

    app.save_config_to_path(str(config_path))
    assert config_path.exists()

    fresh = gui_module.App()
    fresh.withdraw()
    try:
        fresh.load_config_from_path(str(config_path))
        assert fresh.server_var.get() == "testserver.example.edu"
        assert fresh.database_var.get() == "testdb"
        assert fresh.driver_var.get() == "ODBC Driver 18 for SQL Server"
        assert fresh.username_var.get() == "myuser"
        assert fresh.password_var.get() == "mypassword"
        assert fresh.trusted_var.get() is False
        assert str(fresh.username_entry.cget("state")) == "normal"
    finally:
        fresh.destroy()


def test_save_config_trusted_omits_credentials(app, tmp_path):
    config_path = tmp_path / "trusted.ini"
    app.server_var.set("someserver")
    app.trusted_var.set(True)
    app.username_var.set("shouldnotappear")
    app.password_var.set("shouldnotappear")

    app.save_config_to_path(str(config_path))

    from db2bibtex.exporter import load_config

    cfg = load_config(str(config_path))
    assert not cfg["uid"]
    assert not cfg["pwd"]


def test_run_export_success_path(app, tmp_path):
    fake_result = ExportResult(output_path="out.bib", entries_written=3, skipped_publication_ids=[])

    with patch.object(gui_module, "run_export", return_value=fake_result) as mock_run, patch.object(
        gui_module.messagebox, "showerror"
    ) as mock_showerror:
        app.server_var.set("someserver")
        app.database_var.set("somedb")
        app.query_file_var.set(str(tmp_path / "query.sql"))
        app.trusted_var.set(True)
        app.on_run_export()

        assert app._worker_thread is not None
        app._worker_thread.join(timeout=5)
        assert not app._worker_thread.is_alive()

        app._poll_queue_once = app._poll_queue  # readability alias
        # Drain the queue synchronously (bypass the after() timer for determinism).
        app.update_idletasks()
        for _ in range(10):
            try:
                app._log_queue.get_nowait()
            except Exception:
                break

    mock_run.assert_called_once()
    assert str(app.run_button.cget("state")) in ("disabled", "normal")
    mock_showerror.assert_not_called()


def test_run_export_error_path_shows_messagebox(app, tmp_path):
    with patch.object(
        gui_module, "run_export", side_effect=PyodbcMissingError("pyodbc unavailable")
    ), patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.server_var.set("someserver")
        app.database_var.set("somedb")
        app.query_file_var.set(str(tmp_path / "query.sql"))
        app.trusted_var.set(True)
        app.on_run_export()

        assert app._worker_thread is not None
        app._worker_thread.join(timeout=5)

        # Manually invoke the queue-draining logic once (not via after()) so
        # the test doesn't depend on the tk event loop's timer firing.
        drained = []
        while True:
            try:
                drained.append(app._log_queue.get_nowait())
            except Exception:
                break

    assert any("pyodbc unavailable" in m for m in drained)


def test_run_export_missing_server_shows_messagebox(app):
    app.server_var.set("")
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_export()
    mock_showerror.assert_called_once()
    assert app._worker_thread is None


def test_run_export_missing_database_shows_messagebox(app, tmp_path):
    app.server_var.set("someserver")
    app.database_var.set("")
    app.query_file_var.set(str(tmp_path / "query.sql"))
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_export()
    mock_showerror.assert_called_once()
    assert app._worker_thread is None


def test_run_export_missing_query_file_field_shows_messagebox(app):
    app.server_var.set("someserver")
    app.database_var.set("somedb")
    app.query_file_var.set("")
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_export()
    mock_showerror.assert_called_once()
    assert app._worker_thread is None


def test_about_dialog_includes_nsf_acknowledgement(app):
    with patch.object(gui_module.messagebox, "showinfo") as mock_showinfo:
        app.show_about()
    mock_showinfo.assert_called_once()
    _, message = mock_showinfo.call_args[0]
    assert "DEB-2025755" in message
    assert gui_module.__version__ in message


def test_query_file_has_no_default(app):
    """The public codebase must not silently assume any particular query
    file exists -- the field starts empty, same as Server and Database."""
    assert app.query_file_var.get() == ""


def test_browse_query_file_sets_field(app, tmp_path):
    picked = tmp_path / "custom_query.sql"
    with patch.object(gui_module.filedialog, "askopenfilename", return_value=str(picked)):
        app.on_browse_query_file()
    assert app.query_file_var.get() == str(picked)


def test_browse_query_file_cancelled_leaves_field_unchanged(app):
    with patch.object(gui_module.filedialog, "askopenfilename", return_value=""):
        app.on_browse_query_file()
    assert app.query_file_var.get() == ""


def test_run_export_missing_query_file_shows_friendly_error(app, tmp_path):
    friendly_message = (
        "Query file not found: query.sql\n"
        "Copy query.sql.example to query.sql (or pass --query-file / use the "
        "Query File field) and adapt it to your database's table and column "
        "names before running db2bibtex."
    )
    with patch.object(
        gui_module, "run_export", side_effect=QueryFileMissingError(friendly_message)
    ):
        app.server_var.set("someserver")
        app.database_var.set("somedb")
        # A non-empty path that passes the GUI's own required-field check but
        # still doesn't exist on disk -- run_export (mocked here) is what
        # actually raises QueryFileMissingError when it tries to read it.
        app.query_file_var.set(str(tmp_path / "query.sql"))
        app.trusted_var.set(True)
        app.on_run_export()

        assert app._worker_thread is not None
        app._worker_thread.join(timeout=5)

        drained = []
        while True:
            try:
                drained.append(app._log_queue.get_nowait())
            except Exception:
                break

    # The GUI must surface the actionable message as-is, not a bare
    # traceback and not "export failed: ..." noise wrapped around it.
    assert any(msg == f"ERROR: {friendly_message}" for msg in drained)
    assert not any("Traceback" in msg for msg in drained)


def test_notebook_has_export_compare_and_fix_authors_tabs(app):
    tab_texts = [app.notebook.tab(tab_id, "text") for tab_id in app.notebook.tabs()]
    assert tab_texts == ["Export", "Compare", "Fix Authors"]


def test_browse_backup_sets_field(app, tmp_path):
    picked = tmp_path / "backup.bib"
    with patch.object(gui_module.filedialog, "askopenfilename", return_value=str(picked)):
        app.on_browse_backup()
    assert app.backup_var.get() == str(picked)


def test_browse_library_sets_field(app, tmp_path):
    picked = tmp_path / "library.bib"
    with patch.object(gui_module.filedialog, "askopenfilename", return_value=str(picked)):
        app.on_browse_library()
    assert app.library_var.get() == str(picked)


def test_browse_compare_output_sets_field(app, tmp_path):
    picked = tmp_path / "missing.bib"
    with patch.object(gui_module.filedialog, "asksaveasfilename", return_value=str(picked)):
        app.on_browse_compare_output()
    assert app.compare_output_var.get() == str(picked)


def test_run_compare_missing_backup_shows_messagebox(app):
    app.backup_var.set("")
    app.library_var.set("somelibrary.bib")
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_compare()
    mock_showerror.assert_called_once()
    assert app._compare_worker_thread is None


def test_run_compare_missing_library_shows_messagebox(app, tmp_path):
    app.backup_var.set(str(tmp_path / "backup.bib"))
    app.library_var.set("")
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_compare()
    mock_showerror.assert_called_once()
    assert app._compare_worker_thread is None


def test_run_compare_success_path(app, tmp_path):
    backup_path = tmp_path / "backup.bib"
    library_path = tmp_path / "library.bib"
    output_path = tmp_path / "missing.bib"
    backup_path.write_text(
        "@article{AND100,\n  note = {Source DB: publication_id 100; pub_number 100; catalog_id 1},\n}\n",
        encoding="utf-8",
    )
    library_path.write_text("", encoding="utf-8")

    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.backup_var.set(str(backup_path))
        app.library_var.set(str(library_path))
        app.compare_output_var.set(str(output_path))
        app.on_run_compare()

        assert app._compare_worker_thread is not None
        app._compare_worker_thread.join(timeout=5)
        assert not app._compare_worker_thread.is_alive()

        app.update_idletasks()
        drained = []
        while True:
            try:
                drained.append(app._compare_log_queue.get_nowait())
            except Exception:
                break

    mock_showerror.assert_not_called()
    assert output_path.exists()
    assert any("Missing from library: 1 entries" in m for m in drained)


def test_run_compare_error_path_shows_messagebox(app, tmp_path):
    app.backup_var.set(str(tmp_path / "does_not_exist_backup.bib"))
    app.library_var.set(str(tmp_path / "does_not_exist_library.bib"))
    app.compare_output_var.set(str(tmp_path / "missing.bib"))

    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_compare()

        assert app._compare_worker_thread is not None
        app._compare_worker_thread.join(timeout=5)

        drained = []
        while True:
            try:
                drained.append(app._compare_log_queue.get_nowait())
            except Exception:
                break

    assert any(msg.startswith("ERROR:") for msg in drained)


# ---------------------------------------------------------------------------
# Fix Authors tab
# ---------------------------------------------------------------------------

def _set_valid_fix_fields(app):
    app.zotero_library_id_var.set("12345")
    app.zotero_library_type_var.set("group")
    app.zotero_api_key_var.set("secret-key")
    app.crossref_mailto_var.set("someone@example.org")


def test_api_key_visibility_toggle(app):
    assert app.api_key_entry.cget("show") == "*"
    app.show_api_key_var.set(True)
    app.on_show_api_key_toggle()
    assert app.api_key_entry.cget("show") == ""
    app.show_api_key_var.set(False)
    app.on_show_api_key_toggle()
    assert app.api_key_entry.cget("show") == "*"


def test_browse_audit_path_sets_field(app, tmp_path):
    picked = tmp_path / "custom_audit.xlsx"
    with patch.object(gui_module.filedialog, "asksaveasfilename", return_value=str(picked)):
        app.on_browse_audit_path()
    assert app.fix_audit_path_var.get() == str(picked)


def test_browse_audit_path_cancelled_leaves_field_unchanged(app):
    original = app.fix_audit_path_var.get()
    with patch.object(gui_module.filedialog, "asksaveasfilename", return_value=""):
        app.on_browse_audit_path()
    assert app.fix_audit_path_var.get() == original


def test_run_fix_authors_missing_library_id_shows_messagebox(app):
    _set_valid_fix_fields(app)
    app.zotero_library_id_var.set("")
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_fix_authors()
    mock_showerror.assert_called_once()
    assert app._fix_worker_thread is None


def test_run_fix_authors_missing_library_type_shows_messagebox(app):
    _set_valid_fix_fields(app)
    app.zotero_library_type_var.set("")
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_fix_authors()
    mock_showerror.assert_called_once()
    assert app._fix_worker_thread is None


def test_run_fix_authors_missing_api_key_shows_messagebox(app):
    _set_valid_fix_fields(app)
    app.zotero_api_key_var.set("")
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_fix_authors()
    mock_showerror.assert_called_once()
    assert app._fix_worker_thread is None


def test_run_fix_authors_missing_crossref_mailto_shows_messagebox(app):
    _set_valid_fix_fields(app)
    app.crossref_mailto_var.set("")
    with patch.object(gui_module.messagebox, "showerror") as mock_showerror:
        app.on_run_fix_authors()
    mock_showerror.assert_called_once()
    assert app._fix_worker_thread is None


def test_run_fix_authors_dry_run_does_not_prompt_confirmation(app, tmp_path):
    _set_valid_fix_fields(app)
    app.fix_live_var.set(False)
    app.fix_audit_path_var.set(str(tmp_path / "audit.xlsx"))
    fake_result = RunResult(rows=[], counts={})

    with patch.object(
        gui_module, "run_fix_authors", return_value=fake_result
    ) as mock_run, patch.object(gui_module.messagebox, "askyesno") as mock_askyesno:
        app.on_run_fix_authors()
        assert app._fix_worker_thread is not None
        app._fix_worker_thread.join(timeout=5)

    mock_askyesno.assert_not_called()
    mock_run.assert_called_once()
    called_cfg = mock_run.call_args[0][0]
    assert called_cfg.dry_run is True


def test_run_fix_authors_live_without_collection_warns_and_respects_no(app, tmp_path):
    _set_valid_fix_fields(app)
    app.fix_live_var.set(True)
    app.fix_collection_var.set("")
    app.fix_audit_path_var.set(str(tmp_path / "audit.xlsx"))

    with patch.object(gui_module, "run_fix_authors") as mock_run, patch.object(
        gui_module.messagebox, "askyesno", return_value=False
    ) as mock_askyesno:
        app.on_run_fix_authors()

    mock_askyesno.assert_called_once()
    mock_run.assert_not_called()
    assert app._fix_worker_thread is None


def test_run_fix_authors_live_confirmed_runs(app, tmp_path):
    _set_valid_fix_fields(app)
    app.fix_live_var.set(True)
    app.fix_collection_var.set("COLLKEY")
    app.fix_audit_path_var.set(str(tmp_path / "audit.xlsx"))
    fake_result = RunResult(
        rows=[AuditRow("A", "Title", "10.1/a", "Old", "New", "updated")], counts={"updated": 1}
    )

    with patch.object(
        gui_module, "run_fix_authors", return_value=fake_result
    ) as mock_run, patch.object(gui_module.messagebox, "askyesno", return_value=True):
        app.on_run_fix_authors()
        assert app._fix_worker_thread is not None
        app._fix_worker_thread.join(timeout=5)

        drained = []
        while True:
            try:
                drained.append(app._fix_log_queue.get_nowait())
            except Exception:
                break

    mock_run.assert_called_once()
    called_cfg = mock_run.call_args[0][0]
    assert called_cfg.dry_run is False
    assert called_cfg.collection_key == "COLLKEY"
    assert any("DONE" in m for m in drained)


def test_run_fix_authors_error_path_shows_messagebox(app, tmp_path):
    _set_valid_fix_fields(app)
    app.fix_audit_path_var.set(str(tmp_path / "audit.xlsx"))

    with patch.object(
        gui_module, "run_fix_authors", side_effect=ZoteroDepsMissingError("pyzotero unavailable")
    ):
        app.on_run_fix_authors()
        assert app._fix_worker_thread is not None
        app._fix_worker_thread.join(timeout=5)

        drained = []
        while True:
            try:
                drained.append(app._fix_log_queue.get_nowait())
            except Exception:
                break

    assert any("pyzotero unavailable" in m for m in drained)


def test_run_fix_authors_already_running_shows_warning(app, tmp_path):
    _set_valid_fix_fields(app)
    app.fix_audit_path_var.set(str(tmp_path / "audit.xlsx"))
    app._fix_worker_thread = MagicMock()
    app._fix_worker_thread.is_alive.return_value = True

    with patch.object(gui_module.messagebox, "showwarning") as mock_showwarning:
        app.on_run_fix_authors()

    mock_showwarning.assert_called_once()


def test_shared_config_round_trip_preserves_both_sections(app, tmp_path):
    """Saving from one tab must not clobber the other tab's section in the
    same shared db_config.ini file."""
    config_path = tmp_path / "shared.ini"

    app.server_var.set("testserver.example.edu")
    app.database_var.set("testdb")
    app.trusted_var.set(True)
    app.zotero_library_id_var.set("54321")
    app.zotero_library_type_var.set("user")
    app.zotero_api_key_var.set("zkey")
    app.crossref_mailto_var.set("me@example.org")

    app.save_config_to_path(str(config_path))

    fresh = gui_module.App()
    fresh.withdraw()
    try:
        fresh.load_config_from_path(str(config_path))
        assert fresh.server_var.get() == "testserver.example.edu"
        assert fresh.database_var.get() == "testdb"
        assert fresh.zotero_library_id_var.get() == "54321"
        assert fresh.zotero_library_type_var.get() == "user"
        assert fresh.zotero_api_key_var.get() == "zkey"
        assert fresh.crossref_mailto_var.get() == "me@example.org"
    finally:
        fresh.destroy()
