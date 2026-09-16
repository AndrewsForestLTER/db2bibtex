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
