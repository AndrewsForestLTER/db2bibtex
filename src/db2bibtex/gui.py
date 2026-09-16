"""Tkinter GUI for db2bibtex (stdlib only)."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from db2bibtex import __version__
from db2bibtex.exporter import (
    DEFAULT_DRIVER,
    PyodbcMissingError,
    QueryFileMissingError,
    load_config,
    run_export,
    save_config,
)

ABOUT_TEXT = (
    "db2bibtex {version}\n\n"
    "Export Published H.J. Andrews Experimental Forest / LTER publications "
    "from SQL Server to BibTeX for import into Zotero.\n\n"
    "This material is based upon work supported by the H.J. Andrews "
    "Experimental Forest and Long Term Ecological Research (LTER) program "
    "under the NSF grant LTER8 DEB-2025755."
)


class App(tk.Tk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("db2bibtex")
        self.resizable(True, True)

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._worker_thread: threading.Thread | None = None

        self.server_var = tk.StringVar()
        self.database_var = tk.StringVar()
        self.driver_var = tk.StringVar(value=DEFAULT_DRIVER)
        self.query_file_var = tk.StringVar()
        self.trusted_var = tk.BooleanVar(value=False)
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.show_password_var = tk.BooleanVar(value=False)
        self.before_year_var = tk.IntVar(value=1980)
        self.output_var = tk.StringVar(value="publications_before_1980.bib")

        self._build_menu()
        self._build_form()
        self._build_log_pane()

        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Load Config...", command=self.on_load_config)
        file_menu.add_command(label="Save Config...", command=self.on_save_config)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    def _build_form(self) -> None:
        frame = ttk.Frame(self, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")

        row = 0
        ttk.Label(frame, text="Server").grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.server_var, width=40).grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(frame, text="Database").grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.database_var, width=40).grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(frame, text="Driver").grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.driver_var, width=40).grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(frame, text="Query file").grid(row=row, column=0, sticky="w")
        qf_frame = ttk.Frame(frame)
        qf_frame.grid(row=row, column=1, sticky="ew")
        ttk.Entry(qf_frame, textvariable=self.query_file_var, width=32).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(qf_frame, text="Browse...", command=self.on_browse_query_file).pack(side="left")
        row += 1

        self.trusted_check = ttk.Checkbutton(
            frame,
            text="Use Windows trusted connection",
            variable=self.trusted_var,
            command=self.on_trusted_toggle,
        )
        self.trusted_check.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Label(frame, text="Username").grid(row=row, column=0, sticky="w")
        self.username_entry = ttk.Entry(frame, textvariable=self.username_var, width=40)
        self.username_entry.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(frame, text="Password").grid(row=row, column=0, sticky="w")
        pw_frame = ttk.Frame(frame)
        pw_frame.grid(row=row, column=1, sticky="ew")
        self.password_entry = ttk.Entry(pw_frame, textvariable=self.password_var, show="*", width=32)
        self.password_entry.pack(side="left", fill="x", expand=True)
        self.show_password_check = ttk.Checkbutton(
            pw_frame, text="Show", variable=self.show_password_var, command=self.on_show_password_toggle
        )
        self.show_password_check.pack(side="left")
        row += 1

        ttk.Label(frame, text="Before year").grid(row=row, column=0, sticky="w")
        ttk.Spinbox(
            frame, from_=1800, to=2100, textvariable=self.before_year_var, width=10
        ).grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(frame, text="Output file").grid(row=row, column=0, sticky="w")
        out_frame = ttk.Frame(frame)
        out_frame.grid(row=row, column=1, sticky="ew")
        ttk.Entry(out_frame, textvariable=self.output_var, width=32).pack(side="left", fill="x", expand=True)
        ttk.Button(out_frame, text="Browse...", command=self.on_browse_output).pack(side="left")
        row += 1

        self.run_button = ttk.Button(frame, text="Run Export", command=self.on_run_export)
        self.run_button.grid(row=row, column=0, columnspan=2, pady=(10, 0))
        row += 1

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        frame.columnconfigure(1, weight=1)

        self.on_trusted_toggle()

    def _build_log_pane(self) -> None:
        log_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        log_frame.grid(row=1, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    # Interactive logic
    # ------------------------------------------------------------------
    def on_trusted_toggle(self) -> None:
        """Enable/disable username+password fields based on trusted-connection state."""
        state = "disabled" if self.trusted_var.get() else "normal"
        self.username_entry.configure(state=state)
        self.password_entry.configure(state=state)

    def on_show_password_toggle(self) -> None:
        """Toggle password field masking."""
        self.password_entry.configure(show="" if self.show_password_var.get() else "*")

    def on_browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".bib",
            filetypes=[("BibTeX files", "*.bib"), ("All files", "*.*")],
            initialfile=self.output_var.get(),
        )
        if path:
            self.output_var.set(path)

    def on_browse_query_file(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("SQL files", "*.sql"), ("All files", "*.*")],
            initialfile=self.query_file_var.get(),
        )
        if path:
            self.query_file_var.set(path)

    def on_load_config(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Config files", "*.ini"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.load_config_from_path(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Load Config Failed", str(exc))

    def load_config_from_path(self, path: str) -> None:
        """Load an .ini config file into the form fields."""
        cfg = load_config(path)
        if cfg.get("server"):
            self.server_var.set(cfg["server"])
        if cfg.get("database"):
            self.database_var.set(cfg["database"])
        if cfg.get("driver"):
            self.driver_var.set(cfg["driver"])
        if cfg.get("uid"):
            self.username_var.set(cfg["uid"])
        if cfg.get("pwd"):
            self.password_var.set(cfg["pwd"])
        self.trusted_var.set(not bool(cfg.get("uid")))
        self.on_trusted_toggle()

    def on_save_config(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".ini", filetypes=[("Config files", "*.ini"), ("All files", "*.*")]
        )
        if not path:
            return
        if self.password_var.get() and not self.trusted_var.get():
            if not messagebox.askyesno(
                "Save Plaintext Password?",
                "This will write your database password to disk in plaintext.\n\n"
                "Continue?",
            ):
                return
        try:
            self.save_config_to_path(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save Config Failed", str(exc))

    def save_config_to_path(self, path: str) -> None:
        """Save the current form fields to an .ini config file."""
        save_config(
            path,
            server=self.server_var.get(),
            database=self.database_var.get(),
            driver=self.driver_var.get(),
            uid=None if self.trusted_var.get() else self.username_var.get(),
            pwd=None if self.trusted_var.get() else self.password_var.get(),
            trust_server_certificate=True,
        )

    def show_about(self) -> None:
        messagebox.showinfo("About db2bibtex", ABOUT_TEXT.format(version=__version__))

    def on_run_export(self) -> None:
        """Validate inputs and launch the export on a background thread."""
        server = self.server_var.get().strip()
        if not server:
            messagebox.showerror("Missing Server", "Please enter a database server.")
            return

        database = self.database_var.get().strip()
        if not database:
            messagebox.showerror("Missing Database", "Please enter a database name.")
            return

        query_file = self.query_file_var.get().strip()
        if not query_file:
            messagebox.showerror(
                "Missing Query File",
                "Please choose a query file (copy query.sql.example to "
                "query.sql and adapt it, then Browse to it here).",
            )
            return

        if self._worker_thread and self._worker_thread.is_alive():
            messagebox.showwarning("Export Running", "An export is already in progress.")
            return

        kwargs = dict(
            server=server,
            database=database,
            before_year=self.before_year_var.get(),
            output_path=self.output_var.get().strip()
            or f"publications_before_{self.before_year_var.get()}.bib",
            driver=self.driver_var.get().strip() or DEFAULT_DRIVER,
            trusted_connection=self.trusted_var.get(),
            uid=None if self.trusted_var.get() else self.username_var.get().strip(),
            pwd=None if self.trusted_var.get() else self.password_var.get(),
            trust_server_certificate=True,
            query_file=query_file,
        )

        self.run_button.configure(state="disabled")
        self.progress.start(10)
        self._worker_thread = threading.Thread(target=self._run_worker, kwargs=kwargs, daemon=True)
        self._worker_thread.start()

    def _run_worker(self, **kwargs) -> None:
        """Runs in a background thread: performs the export, logs via the queue."""
        try:
            result = run_export(progress_callback=self._log_queue.put, **kwargs)
            self._log_queue.put(
                f"DONE: wrote {result.entries_written} entries to {result.output_path}"
            )
        except PyodbcMissingError as exc:
            self._log_queue.put(f"ERROR: {exc}")
        except QueryFileMissingError as exc:
            self._log_queue.put(f"ERROR: {exc}")
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
            self._log_queue.put(f"ERROR: export failed: {exc}")
        finally:
            self._log_queue.put("__EXPORT_FINISHED__")

    def _poll_queue(self) -> None:
        """Drain the log queue into the log pane; reschedules itself via after()."""
        try:
            while True:
                msg = self._log_queue.get_nowait()
                if msg == "__EXPORT_FINISHED__":
                    self.run_button.configure(state="normal")
                    self.progress.stop()
                    continue
                self._append_log(msg)
                if msg.startswith("ERROR:"):
                    messagebox.showerror("Export Failed", msg[len("ERROR: ") :])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    """GUI entry point."""
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
