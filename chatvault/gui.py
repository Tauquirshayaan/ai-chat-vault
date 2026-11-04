"""Desktop window for AI Chat Vault (tkinter, optional Sun Valley theme)."""

import os
import queue
import sys
import threading
import time
import traceback
import webbrowser

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .common import (IS_WINDOWS, default_env, drive_of, human_size, running_apps,
                     suggest_backup_folder, system_drive)

REPO_URL = "https://github.com/Tauquirshayaan/ai-chat-vault"
APP_NAME = "AI Chat Vault"
TOOLS = (
    ("antigravity", "Antigravity", "Chats, brain notes, sidebar index and settings"),
    ("codex", "Codex", "Sessions, history, memories and configuration"),
    ("claude", "Claude Code", "Transcripts, memory, settings, skills and MCP servers"),
)
TOOL_NAMES = {key: name for key, name, _ in TOOLS}

PALETTES = {
    "dark": {"bg": "#1c1c1c", "sidebar": "#202020", "hover": "#2b2b2b", "selected": "#2d2d2d",
             "text": "#ffffff", "muted": "#a3a3a3", "accent": "#57c8ff", "border": "#333333",
             "danger": "#ff99a4", "success": "#6ccb5f", "warning": "#fce100",
             "log_bg": "#141414", "log_fg": "#d4d4d4", "banner": "#433519"},
    "light": {"bg": "#fafafa", "sidebar": "#f0f0f0", "hover": "#e6e6e6", "selected": "#e0e0e0",
              "text": "#1a1a1a", "muted": "#5f5f5f", "accent": "#005fb8", "border": "#d9d9d9",
              "danger": "#c42b1c", "success": "#0f7b0f", "warning": "#9d5d00",
              "log_bg": "#ffffff", "log_fg": "#1a1a1a", "banner": "#fff4ce"},
}


# ─── Platform polish ─────────────────────────────────────────────────────────

def _enable_dpi_awareness():
    """Sharp text on high-resolution Windows screens."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _windows_prefers_dark():
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def _dark_title_bar(window, dark):
    if not (IS_WINDOWS and dark):
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def _open_path(path):
    try:
        if IS_WINDOWS:
            os.startfile(path)  # noqa: S606 - opens the user's own file or folder
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except Exception as error:
        messagebox.showerror(APP_NAME, f"Could not open {path}:\n{error}")


# ─── Background tasks ────────────────────────────────────────────────────────

class _QueueWriter:
    """Sends print() output from the engine to the window's details log."""

    def __init__(self, events):
        self.events = events

    def write(self, text):
        if text:
            self.events.put(("log", text))
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class TaskRunner:
    def __init__(self, app):
        self.app = app
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.label = ""
        self.uncancellable = ()
        self._on_done = None

    def start(self, title, work, on_done, uncancellable=("Restoring files",)):
        """Run work(progress) in a thread; on_done(status, result) on the UI thread."""
        if self.busy:
            messagebox.showinfo(APP_NAME, "Please wait for the current task to finish.")
            return
        self.busy = True
        self.label = title
        self.uncancellable = uncancellable
        self.cancel_event.clear()
        self._on_done = on_done
        self.app.set_busy(True, title)
        self.app.log(f"\n── {title} · {time.strftime('%H:%M:%S')} ──\n")
        threading.Thread(target=self._run, args=(work,), daemon=True).start()

    def _progress(self, done, total, label):
        from .archive import Cancelled
        self.label = label
        if self.cancel_event.is_set() and label not in self.uncancellable:
            raise Cancelled()
        self.events.put(("progress", done, total, label))

    def _run(self, work):
        from .archive import Cancelled
        try:
            result = work(self._progress)
            self.events.put(("done", "ok", result))
        except Cancelled:
            self.events.put(("done", "cancelled", None))
        except Exception as error:
            self.events.put(("log", traceback.format_exc()))
            self.events.put(("done", "error", error))

    def cancel(self):
        if self.busy and self.label not in self.uncancellable:
            self.cancel_event.set()
            self.app.status_text.set("Cancelling…")

    def poll(self):
        latest_progress = None
        log_parts = []
        finished = None
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    log_parts.append(event[1])
                elif event[0] == "progress":
                    latest_progress = event
                elif event[0] == "done":
                    finished = event
                elif event[0] == "call":
                    event[1]()
        except queue.Empty:
            pass
        if log_parts:
            self.app.log("".join(log_parts))
        if latest_progress and self.busy:
            _, done, total, label = latest_progress
            self.app.show_progress(done, total, label)
        if finished:
            _, status, result = finished
            self.busy = False
            self.app.set_busy(False)
            if self._on_done:
                self._on_done(status, result)


# ─── Reusable widgets ────────────────────────────────────────────────────────

class Page(ttk.Frame):
    title = ""
    subtitle = ""

    def __init__(self, app):
        super().__init__(app.content)
        self.app = app
        # Scrollable page: a canvas holding the content, scrollbar shown only
        # when the content is taller than the window.
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background=app.frame_background)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.inner = ttk.Frame(self.canvas, padding=(36, 28, 36, 20))
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._window, width=e.width))

        ttk.Label(self.inner, text=self.title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.inner, text=self.subtitle, style="Muted.TLabel", wraplength=680,
                  justify="left").pack(anchor="w", pady=(4, 16))
        self.body = ttk.Frame(self.inner)
        self.body.pack(fill="both", expand=True)

    def _on_scroll(self, first, last):
        self.vbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.vbar.grid_remove()
        else:
            self.vbar.grid()

    def scroll(self, units):
        if self.vbar.winfo_ismapped():
            self.canvas.yview_scroll(units, "units")

    def scroll_to_end(self):
        def go():
            self.update_idletasks()  # let the new result take its size first
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            self.canvas.yview_moveto(1.0)
        self.after(60, go)

    def on_show(self):
        pass

    def card(self, parent, title):
        frame = ttk.LabelFrame(parent, text=f" {title} ", padding=(16, 10, 16, 14))
        frame.pack(fill="x", pady=(0, 14))
        return frame

    def tool_checks(self, parent, variables, command=None, descriptions=True):
        for key, name, desc in TOOLS:
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            ttk.Checkbutton(row, text=name, variable=variables[key],
                            command=command).pack(side="left")
            if descriptions:
                ttk.Label(row, text="  " + desc, style="Muted.TLabel").pack(side="left")

    def path_row(self, parent, variable, browse, button_text="Browse…"):
        row = ttk.Frame(parent)
        row.pack(fill="x")
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text=button_text, command=browse).pack(side="left", padx=(8, 0))
        return entry


class ResultPanel(ttk.Frame):
    """A message box inside the page, with optional action buttons."""

    def __init__(self, parent):
        super().__init__(parent)
        self.headline = ttk.Label(self, style="ResultTitle.TLabel")
        self.headline.pack(anchor="w")
        self.detail = ttk.Label(self, style="Muted.TLabel", wraplength=680, justify="left")
        self.detail.pack(anchor="w", pady=(2, 6))
        self.actions = ttk.Frame(self)
        self.actions.pack(anchor="w")

    def show(self, kind, headline, detail="", actions=()):
        style = {"success": "Success.TLabel", "error": "Danger.TLabel",
                 "warning": "Warning.TLabel"}.get(kind, "Heading.TLabel")
        self.headline.configure(text=headline, style=style)
        self.detail.configure(text=detail)
        for child in self.actions.winfo_children():
            child.destroy()
        for text, command in actions:
            ttk.Button(self.actions, text=text, command=command).pack(side="left", padx=(0, 8))
        self.pack(fill="x", pady=(10, 0))
        page = self.master
        while page is not None and not isinstance(page, Page):
            page = page.master
        if page is not None:
            page.scroll_to_end()

    def hide(self):
        self.pack_forget()


def _pick_backup_values():
    from .cli import find_backups
    return find_backups()


# ─── Pages ───────────────────────────────────────────────────────────────────

class BackupPage(Page):
    title = "Back up your chats"
    subtitle = ("Saves your Antigravity, Codex and Claude Code chats into one verified zip "
                "that you can restore after formatting or on a new PC.")

    def __init__(self, app):
        super().__init__(app)
        self.plan = None
        self.scanning = False
        self.tool_vars = {k: tk.BooleanVar(value=True) for k, _, _ in TOOLS}
        self.ext_var = tk.BooleanVar(value=False)
        self.rec_var = tk.BooleanVar(value=False)
        self.readable_var = tk.BooleanVar(value=True)
        self.logins_var = tk.BooleanVar(value=False)
        self.dest_var = tk.StringVar(value=suggest_backup_folder())
        self.dest_var.trace_add("write", lambda *_: self._update_destination())
        self._row_checks = {}

        self.banner = ttk.Label(self.body, style="Banner.TLabel", wraplength=700,
                                justify="left", padding=(12, 8))

        include = self.card(self.body, "What to include")
        self.size_labels = {}
        for key, name, desc in TOOLS:
            self.size_labels[key] = self._check_row(include, name, desc, self.tool_vars[key])
        ttk.Separator(include).pack(fill="x", pady=8)
        self.ext_size = self._check_row(include, "Editor extensions",
                                        "Antigravity extensions; they can also be reinstalled",
                                        self.ext_var)
        self.rec_size = self._check_row(include, "Browser-agent screenshots",
                                        "Page images saved by Antigravity's browser agent",
                                        self.rec_var)
        self._check_row(include, "Readable copy of every chat",
                        "Markdown files you can open in any editor or give to a new agent",
                        self.readable_var)
        self._check_row(include, "Login tokens",
                        "Only if this backup never leaves your own drives",
                        self.logins_var)

        save = self.card(self.body, "Save to")
        self.path_row(save, self.dest_var, self._browse)
        self.dest_note = ttk.Label(save, style="Muted.TLabel")
        self.dest_note.pack(anchor="w", pady=(6, 0))

        footer = ttk.Frame(self.body)
        footer.pack(fill="x", pady=(2, 0))
        self.total_label = ttk.Label(footer, text="Scanning your folders…", style="Heading.TLabel")
        self.total_label.pack(side="left")
        self.start_button = ttk.Button(footer, text="Start backup", style="Accent.TButton",
                                       command=self.start, width=18)
        self.start_button.pack(side="right")
        app.register_action(self.start_button)
        self.result = ResultPanel(self.body)
        self._update_destination()

    def _check_row(self, parent, name, desc, variable):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Checkbutton(row, text=name, variable=variable,
                        command=self._update_total).pack(side="left")
        ttk.Label(row, text="  " + desc, style="Muted.TLabel").pack(side="left")
        size = ttk.Label(row, text="", style="Muted.TLabel")
        size.pack(side="right")
        self._row_checks[size] = row.winfo_children()[0]
        return size

    def on_show(self):
        self._refresh_banner()
        if self.plan is None and not self.scanning:
            self.rescan()

    def _refresh_banner(self):
        def check():
            busy = running_apps()
            self.app.call_soon(lambda: self._set_banner(busy))
        threading.Thread(target=check, daemon=True).start()

    def _set_banner(self, busy):
        if busy:
            self.banner.configure(text="⚠  Still open: " + ", ".join(busy) + ". Close them "
                                  "for the most complete backup. Databases are still copied "
                                  "safely, but a chat being written right now may be cut short.")
            self.banner.pack(fill="x", pady=(0, 14), before=self.body.winfo_children()[1])
        else:
            self.banner.pack_forget()

    def rescan(self):
        self.scanning = True
        for label in list(self.size_labels.values()) + [self.ext_size, self.rec_size]:
            label.configure(text="scanning…")
        self.total_label.configure(text="Scanning your folders…")
        self.start_button.configure(state="disabled")

        def scan():
            from .archive import plan_backup
            try:
                plan = plan_backup(default_env(), None, True, True)
            except Exception as error:
                plan = error
            self.app.call_soon(lambda: self._scan_done(plan))
        threading.Thread(target=scan, daemon=True).start()

    def _scan_done(self, plan):
        self.scanning = False
        if isinstance(plan, Exception):
            self.total_label.configure(text=f"Could not scan: {plan}")
            return
        from .archive import filter_plan, optional_sizes
        self.plan = plan
        for key, _, _ in TOOLS:
            part = filter_plan(plan, [key], False, False, False)
            self.size_labels[key].configure(
                text=human_size(part["bytes"]) if part["files"] else "not found on this PC")
            if not part["files"]:
                self.tool_vars[key].set(False)
        sizes = optional_sizes(plan)
        for label, var, key in ((self.ext_size, self.ext_var, "extensions"),
                                (self.rec_size, self.rec_var, "recordings")):
            size = sizes.get(key, 0)
            label.configure(text=human_size(size) if size else "none on this PC")
            if not size:
                var.set(False)
                self._row_checks[label].configure(state="disabled")
        if not self.app.runner.busy:
            self.start_button.configure(state="normal")
        self._update_total()

    def _selected_tools(self):
        return [k for k, _, _ in TOOLS if self.tool_vars[k].get()]

    def _narrowed(self):
        from .archive import filter_plan
        return filter_plan(self.plan, self._selected_tools(), self.ext_var.get(),
                           self.rec_var.get(), self.logins_var.get())

    def _update_total(self):
        if self.plan is None:
            return
        plan = self._narrowed()
        self.total_label.configure(
            text=f"{plan['files']:,} files · {human_size(plan['bytes'])} before compression")

    def _update_destination(self):
        dest = self.dest_var.get().strip()
        if not dest:
            self.dest_note.configure(text="Choose a folder.", style="Danger.TLabel")
            return
        sysdrive = system_drive()
        if sysdrive and drive_of(dest) == sysdrive:
            self.dest_note.configure(
                text=f"{sysdrive} is the Windows drive. If you format it, this backup is erased "
                     "too. Choose another drive.", style="Danger.TLabel")
            return
        probe = dest
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            import shutil
            free = shutil.disk_usage(probe).free
            self.dest_note.configure(text=f"{human_size(free)} free on this drive. "
                                          "Copy the finished zip to a USB drive or cloud "
                                          "storage too.", style="Muted.TLabel")
        except OSError:
            self.dest_note.configure(text="This folder cannot be reached.", style="Danger.TLabel")

    def _browse(self):
        folder = filedialog.askdirectory(title="Choose where to save the backup",
                                         initialdir=self.dest_var.get() or None)
        if folder:
            self.dest_var.set(os.path.normpath(folder))

    def start(self):
        if self.plan is None:
            return
        tools = self._selected_tools()
        if not tools:
            messagebox.showwarning(APP_NAME, "Select at least one tool to back up.")
            return
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showwarning(APP_NAME, "Choose a folder to save the backup in.")
            return
        busy = running_apps()
        if busy and not messagebox.askyesno(
                APP_NAME, "These apps are still open:\n\n    " + "\n    ".join(busy)
                + "\n\nFor the most complete backup, close them first.\n\nBack up anyway?",
                icon="warning", default="no"):
            return
        sysdrive = system_drive()
        if sysdrive and drive_of(dest) == sysdrive and not messagebox.askyesno(
                APP_NAME, f"{dest} is on {sysdrive}, the Windows drive.\n\nIf you format that "
                "drive, this backup is erased with it.\n\nUse it anyway?",
                icon="warning", default="no"):
            return

        plan = self._narrowed()
        readable = self.readable_var.get()
        self.result.hide()

        def work(progress):
            from .archive import write_backup
            return write_backup(plan, dest, readable=readable, progress=progress)

        def done(status, result):
            if status == "ok":
                path, manifest = result
                size = human_size(os.path.getsize(path))
                skipped = len(manifest["skipped"])
                detail = (f"{path}\n{len(manifest['files']):,} files · {size} compressed · "
                          "every file verified against its checksum.")
                if skipped:
                    detail += f"\n{skipped} locked file(s) were skipped; see Details."
                detail += ("\nNext: copy this zip to a USB drive or cloud storage as a second "
                           "copy.")
                self.result.show("success", "✓  Backup complete", detail, [
                    ("Open folder", lambda: _open_path(os.path.dirname(path)))])
            elif status == "cancelled":
                self.result.show("warning", "Backup cancelled",
                                 "No backup file was left behind.")
            else:
                self.result.show("error", "Backup failed", str(result))
        self.app.runner.start("Backing up", work, done)


class _BackupPicker:
    """Combobox of found backups plus Browse."""

    def __init__(self, page, parent, on_change):
        self.page = page
        self.on_change = on_change
        self.var = tk.StringVar()
        row = ttk.Frame(parent)
        row.pack(fill="x")
        self.combo = ttk.Combobox(row, textvariable=self.var)
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", lambda _e: on_change())
        self.combo.bind("<FocusOut>", lambda _e: on_change())
        ttk.Button(row, text="Browse…", command=self._browse).pack(side="left", padx=(8, 0))

    def refresh(self):
        found = _pick_backup_values()
        self.combo.configure(values=found)
        if found and not self.var.get():
            self.var.set(found[0])
            self.on_change()
        return found

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Choose a backup", filetypes=[("AI Chat Vault backup", "*.zip"), ("All files", "*")],
            initialdir=os.path.dirname(self.var.get()) if self.var.get() else suggest_backup_folder())
        if path:
            self.var.set(os.path.normpath(path))
            self.on_change()

    def path(self):
        return self.var.get().strip().strip('"')


class RestorePage(Page):
    title = "Restore a backup"
    subtitle = ("Puts your chats back after reinstalling. Install the apps, sign in, open each "
                "once, then close them all before restoring.")

    def __init__(self, app):
        super().__init__(app)
        self.manifest = None
        card = self.card(self.body, "Backup file")
        self.picker = _BackupPicker(self, card, self._load)
        self.info = ttk.Label(card, style="Muted.TLabel", justify="left", wraplength=680)
        self.info.pack(anchor="w", pady=(8, 0))

        tools = self.card(self.body, "Restore these tools")
        self.tool_vars = {k: tk.BooleanVar(value=True) for k, _, _ in TOOLS}
        self.tool_checks_frame = tools
        self.checks = {}
        for key, name, desc in TOOLS:
            row = ttk.Frame(tools)
            row.pack(fill="x", pady=2)
            self.checks[key] = ttk.Checkbutton(row, text=name, variable=self.tool_vars[key])
            self.checks[key].pack(side="left")
            ttk.Label(row, text="  " + desc, style="Muted.TLabel").pack(side="left")

        safety = self.card(self.body, "How restore protects you")
        for line in ("The backup is checked first; a damaged backup is never restored.",
                     "Files it replaces are saved first in a 'before-restore' zip, so it can be "
                     "undone.",
                     "Chats you created after reinstalling are kept.",
                     "Original file dates are restored, so chats stay in the right order."):
            ttk.Label(safety, text="•  " + line, style="Muted.TLabel").pack(anchor="w")

        footer = ttk.Frame(self.body)
        footer.pack(fill="x", pady=(2, 0))
        self.restore_button = ttk.Button(footer, text="Restore", style="Accent.TButton",
                                         command=self.restore, width=18)
        self.restore_button.pack(side="right")
        self.preview_button = ttk.Button(footer, text="Preview changes", command=self.preview)
        self.preview_button.pack(side="right", padx=(0, 8))
        app.register_action(self.restore_button, self.preview_button)
        self.result = ResultPanel(self.body)

    def on_show(self):
        if not self.picker.refresh() and not self.picker.path():
            self.info.configure(text="No backup found in \\AI-Chat-Backups on any drive. "
                                     "Choose one with Browse…")

    def _load(self):
        path = self.picker.path()
        self.manifest = None
        if not path:
            return
        if not os.path.isfile(path):
            self.info.configure(text="File not found.", style="Danger.TLabel")
            return
        import zipfile
        from .archive import BackupError, load_manifest
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = load_manifest(zf)
        except (BackupError, OSError, zipfile.BadZipFile) as error:
            self.info.configure(text=str(error), style="Danger.TLabel")
            return
        self.manifest = manifest
        counts = {}
        for entry in manifest["files"]:
            counts.setdefault(entry["root"], [0, 0])
            counts[entry["root"]][0] += 1
            counts[entry["root"]][1] += entry["size"]
        per_tool = {}
        for root in manifest["roots"]:
            c = counts.get(root["id"], (0, 0))
            agg = per_tool.setdefault(root["tool"], [0, 0])
            agg[0] += c[0]
            agg[1] += c[1]
        lines = [f"Created {manifest['created'][:16].replace('T', ' ')} on {manifest['hostname']}"]
        lines += [f"{tool}: {n:,} files, {human_size(size)}" for tool, (n, size) in per_tool.items()]
        self.info.configure(text="\n".join(lines), style="Muted.TLabel")
        present = set(per_tool)
        for key, name, _ in TOOLS:
            self.checks[key].configure(state="normal" if name in present else "disabled")
            self.tool_vars[key].set(name in present)

    def _tools(self):
        return [k for k, _, _ in TOOLS if self.tool_vars[k].get()
                and str(self.checks[k].cget("state")) != "disabled"]

    def _ready(self):
        if not self.manifest:
            self._load()
        if not self.manifest:
            messagebox.showwarning(APP_NAME, "Choose a valid backup file first.")
            return None
        tools = self._tools()
        if not tools:
            messagebox.showwarning(APP_NAME, "Select at least one tool to restore.")
            return None
        return tools

    def preview(self):
        tools = self._ready()
        if not tools:
            return
        path = self.picker.path()
        self.result.hide()

        def work(progress):
            from .archive import restore_backup
            return restore_backup(path, tools=tools, dry_run=True, progress=progress)

        def done(status, result):
            if status == "ok":
                detail = (f"{result['new']:,} new files · {result['replaced']:,} replaced · "
                          f"{result['unchanged']:,} already identical")
                if result["warnings"]:
                    detail += "\n" + "\n".join(result["warnings"])
                self.result.show("info", "Preview: nothing has been changed", detail)
            elif status == "cancelled":
                self.result.hide()
            else:
                self.result.show("error", "Preview failed", str(result))
        self.app.runner.start("Checking backup", work, done)

    def restore(self):
        tools = self._ready()
        if not tools:
            return
        busy = running_apps()
        if busy:
            messagebox.showerror(APP_NAME, "Close these apps completely first:\n\n    "
                                 + "\n    ".join(busy)
                                 + "\n\nThey would overwrite the restored chats when they exit.")
            return
        names = ", ".join(TOOL_NAMES[t] for t in tools)
        if not messagebox.askyesno(
                APP_NAME, f"Restore {names} from this backup?\n\nFiles that get replaced are "
                "saved first in a 'before-restore' zip next to the backup.", default="yes"):
            return
        path = self.picker.path()
        self.result.hide()

        def work(progress):
            from .archive import restore_backup
            return restore_backup(path, tools=tools, progress=progress)

        def done(status, result):
            if status == "ok":
                detail = (f"{result['new']:,} new · {result['replaced']:,} replaced · "
                          f"{result['unchanged']:,} unchanged.")
                if result["safety_backup"]:
                    detail += f"\nPrevious files saved in {result['safety_backup']}"
                detail += ("\nNext: open the apps and sign in again. Codex: 'codex resume'. "
                           "Claude Code: 'claude --resume' in the project folder. If chats are "
                           "missing from Antigravity's sidebar, use Antigravity repair.")
                actions = []
                if result["safety_backup"]:
                    actions.append(("Open before-restore folder",
                                    lambda: _open_path(os.path.dirname(result["safety_backup"]))))
                self.result.show("success", "✓  Restore complete", detail, actions)
            elif status == "cancelled":
                self.result.show("warning", "Restore cancelled before any file was changed.")
            else:
                self.result.show("error", "Restore failed", str(result))
        self.app.runner.start("Restoring", work, done)


class CheckPage(Page):
    title = "Check a backup"
    subtitle = ("Re-reads every file in a backup and compares it with the checksum saved when "
                "it was made. Nothing is changed.")

    def __init__(self, app):
        super().__init__(app)
        card = self.card(self.body, "Backup file")
        self.picker = _BackupPicker(self, card, lambda: None)
        footer = ttk.Frame(self.body)
        footer.pack(fill="x")
        self.button = ttk.Button(footer, text="Check backup", style="Accent.TButton",
                                 command=self.check, width=18)
        self.button.pack(side="right")
        app.register_action(self.button)
        self.result = ResultPanel(self.body)

    def on_show(self):
        self.picker.refresh()

    def check(self):
        path = self.picker.path()
        if not path or not os.path.isfile(path):
            messagebox.showwarning(APP_NAME, "Choose a backup file first.")
            return
        self.result.hide()

        def work(progress):
            from .archive import verify_backup
            return verify_backup(path, quiet=True, progress=progress)

        def done(status, result):
            if status == "ok":
                ok, problems, manifest = result
                if ok:
                    self.result.show("success", "✓  Backup is intact",
                                     f"All {len(manifest['files']):,} files match their "
                                     f"checksums. Created {manifest['created'][:16].replace('T', ' ')}"
                                     f" on {manifest['hostname']}.")
                else:
                    self.result.show("error", f"Backup is damaged ({len(problems)} problem(s))",
                                     "\n".join(problems[:8]))
            elif status == "cancelled":
                self.result.hide()
            else:
                self.result.show("error", "Could not check this file", str(result))
        self.app.runner.start("Checking backup", work, done)


class ExportPage(Page):
    title = "Readable export"
    subtitle = ("Writes every chat as a Markdown file, grouped by project, with an INDEX.md and "
                "a prompt for briefing a new agent. Your chats are only read, never changed.")

    def __init__(self, app):
        super().__init__(app)
        tools = self.card(self.body, "Chats to export")
        self.tool_vars = {k: tk.BooleanVar(value=True) for k, _, _ in TOOLS}
        self.tool_checks(tools, self.tool_vars, descriptions=False)
        dest = self.card(self.body, "Save to")
        default = os.path.join(suggest_backup_folder(),
                               "AI-Chat-Export_" + time.strftime("%Y-%m-%d_%H%M%S"))
        self.dest_var = tk.StringVar(value=default)
        self.path_row(dest, self.dest_var, self._browse)
        footer = ttk.Frame(self.body)
        footer.pack(fill="x")
        self.button = ttk.Button(footer, text="Export chats", style="Accent.TButton",
                                 command=self.export, width=18)
        self.button.pack(side="right")
        app.register_action(self.button)
        self.result = ResultPanel(self.body)

    def _browse(self):
        folder = filedialog.askdirectory(title="Choose a folder for the export")
        if folder:
            self.dest_var.set(os.path.join(os.path.normpath(folder),
                                           "AI-Chat-Export_" + time.strftime("%Y-%m-%d_%H%M%S")))

    def export(self):
        tools = [k for k, _, _ in TOOLS if self.tool_vars[k].get()]
        dest = self.dest_var.get().strip()
        if not tools or not dest:
            messagebox.showwarning(APP_NAME, "Select at least one tool and a folder.")
            return
        self.result.hide()

        def work(progress):
            from .export import export_all
            progress(0, 0, "Exporting chats")
            return export_all(dest, default_env(), tools)

        def done(status, result):
            if status == "ok":
                parts = ", ".join(f"{n} {TOOL_NAMES[k]}" for k, n in result["by_tool"].items())
                detail = f"{dest}\n{result['chats']} chats: {parts}."
                if result["failed"]:
                    detail += f"\n{len(result['failed'])} file(s) could not be read; see Details."
                self.result.show("success", "✓  Export complete", detail, [
                    ("Open INDEX.md", lambda: _open_path(os.path.join(dest, "INDEX.md"))),
                    ("Open folder", lambda: _open_path(dest))])
            elif status == "cancelled":
                self.result.hide()
            else:
                self.result.show("error", "Export failed", str(result))
        self.app.runner.start("Exporting chats", work, done,
                              uncancellable=("Exporting chats",))


class RepairPage(Page):
    title = "Antigravity sidebar repair"
    subtitle = ("Use this only if chats are missing or out of order in Antigravity's sidebar. "
                "It rebuilds the list from the chats on disk and keeps titles and project links. "
                "Antigravity must be closed.")

    def __init__(self, app):
        super().__init__(app)
        options = self.card(self.body, "Options")
        self.mode_var = tk.StringVar(value="auto")
        ttk.Radiobutton(options, text="Link unlinked chats to their project folders automatically "
                        "(recommended)", value="auto", variable=self.mode_var).pack(anchor="w")
        ttk.Radiobutton(options, text="Leave unlinked chats without a project", value="none",
                        variable=self.mode_var).pack(anchor="w", pady=(2, 6))
        self.keep_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Keep sidebar entries whose chat file is missing",
                        variable=self.keep_var).pack(anchor="w")
        footer = ttk.Frame(self.body)
        footer.pack(fill="x", pady=(0, 14))
        self.button = ttk.Button(footer, text="Repair sidebar", style="Accent.TButton",
                                 command=self.repair, width=18)
        self.button.pack(side="right")

        undo = self.card(self.body, "Undo a repair")
        ttk.Label(undo, text="Every repair saves the previous sidebar list first. Pick one to "
                  "put it back.", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        self.backups = tk.Listbox(undo, height=4, activestyle="none", borderwidth=0,
                                  highlightthickness=1)
        self.backups.pack(fill="x")
        self.undo_button = ttk.Button(undo, text="Restore selected", command=self.undo)
        self.undo_button.pack(anchor="e", pady=(8, 0))
        app.register_action(self.button, self.undo_button)
        app.style_listbox(self.backups)
        self.result = ResultPanel(self.body)
        self.files = []

    def on_show(self):
        import glob
        from . import antigravity_index as ag
        self.files = sorted(glob.glob(os.path.join(ag._backup_dir(),
                                                   "trajectorySummaries_backup*.txt")),
                            key=os.path.getmtime, reverse=True)
        self.backups.delete(0, "end")
        for path in self.files:
            self.backups.insert("end", "  " + os.path.basename(path))
        if not self.files:
            self.backups.insert("end", "  No repairs have been made yet.")

    def _antigravity_closed(self):
        if running_apps(["Antigravity"]):
            messagebox.showerror(APP_NAME, "Close Antigravity completely first "
                                 "(File > Exit, or end it in Task Manager).")
            return False
        return True

    def repair(self):
        if not self._antigravity_closed():
            return
        if not messagebox.askyesno(APP_NAME, "Rebuild Antigravity's sidebar list now?\n\n"
                                   "The current list is saved first, so this can be undone."):
            return
        mode, keep = self.mode_var.get(), self.keep_var.get()
        self.result.hide()

        def work(progress):
            from . import antigravity_index as ag
            progress(0, 0, "Repairing sidebar")
            return ag.run_repair(interactive=False, workspace_mode=mode, keep_missing=keep)

        def done(status, result):
            self.on_show()
            if status == "ok" and result == 0:
                self.result.show("success", "✓  Sidebar repaired",
                                 "Open Antigravity; your chats should appear sorted by date. "
                                 "See Details for the full list.")
            elif status == "ok":
                self.result.show("error", "Repair stopped; nothing was changed",
                                 "See Details for the reason.")
            else:
                self.result.show("error", "Repair failed", str(result))
        self.app.runner.start("Repairing sidebar", work, done, uncancellable=("Repairing sidebar",))

    def undo(self):
        selection = self.backups.curselection()
        if not self.files or not selection:
            messagebox.showinfo(APP_NAME, "Select a saved sidebar list first.")
            return
        if not self._antigravity_closed():
            return
        path = self.files[selection[0]]
        if not messagebox.askyesno(APP_NAME, f"Put this sidebar list back?\n\n"
                                   f"{os.path.basename(path)}"):
            return

        def work(progress):
            from .cli import cmd_undo_repair
            progress(0, 0, "Restoring sidebar list")
            return cmd_undo_repair(path, interactive=False)

        def done(status, result):
            self.on_show()
            if status == "ok" and result == 0:
                self.result.show("success", "✓  Sidebar list restored",
                                 "The list it replaced was saved too, so this can be undone.")
            else:
                self.result.show("error", "Could not restore the sidebar list",
                                 "See Details for the reason.")
        self.app.runner.start("Restoring sidebar list", work, done,
                              uncancellable=("Restoring sidebar list",))


class AboutPage(Page):
    title = "About"
    subtitle = f"{APP_NAME} {__version__} · free and open source (MIT license)"

    def __init__(self, app):
        super().__init__(app)
        privacy = self.card(self.body, "Your privacy")
        for line in ("Works entirely on this PC. It has no network code and never uploads chats.",
                     "Backups are saved only where you choose.",
                     "Login tokens are left out unless you include them.",
                     "A backup holds private conversations: keep it on drives you control."):
            ttk.Label(privacy, text="•  " + line, style="Muted.TLabel").pack(anchor="w")
        steps = self.card(self.body, "Formatting your PC")
        for i, line in enumerate((
                "Close Antigravity, Codex and Claude Code, then run Back up.",
                "Copy the zip to a second place: a USB drive or cloud storage.",
                "After reinstalling: same Windows user name, project drives on the same letters.",
                "Install and sign in to the apps, open each once, close them.",
                "Run Restore."), 1):
            ttk.Label(steps, text=f"{i}.  {line}", style="Muted.TLabel").pack(anchor="w")
        links = ttk.Frame(self.body)
        links.pack(fill="x")
        ttk.Button(links, text="Project on GitHub",
                   command=lambda: webbrowser.open(REPO_URL)).pack(side="left")
        ttk.Button(links, text="Report a problem",
                   command=lambda: webbrowser.open(REPO_URL + "/issues")).pack(side="left",
                                                                             padx=(8, 0))


# ─── Main window ─────────────────────────────────────────────────────────────

class App(tk.Tk):
    PAGES = (("backup", "Back up", BackupPage), ("restore", "Restore", RestorePage),
             ("check", "Check backup", CheckPage), ("export", "Readable export", ExportPage),
             ("repair", "Antigravity repair", RepairPage), ("about", "About", AboutPage))

    def __init__(self, start_page="backup"):
        _enable_dpi_awareness()
        if IS_WINDOWS:
            try:  # own taskbar entry and icon instead of Python's
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AIChatVault.App")
            except Exception:
                pass
        super().__init__()
        self.title(APP_NAME)
        try:
            from ._icon import ICON_32, ICON_64, ICON_128
            self._icons = [tk.PhotoImage(data=data) for data in (ICON_128, ICON_64, ICON_32)]
            self.iconphoto(True, *self._icons)
        except Exception:
            pass
        self.dark, self.themed = self._apply_theme()
        # The theme recolours plain tk widgets when its change event runs; let
        # that happen now so it cannot repaint the sidebar built below.
        self.update()
        self.colors = PALETTES["dark" if self.dark else "light"]
        self.configure(background=self.colors["bg"])
        self.frame_background = ttk.Style(self).lookup("TFrame", "background") or self.colors["bg"]
        self.runner = TaskRunner(self)
        self.status_text = tk.StringVar(value="Ready")
        self._actions = []
        self._nav = {}
        self._details_visible = False
        sys.stdout = _QueueWriter(self.runner.events)
        sys.stderr = _QueueWriter(self.runner.events)

        self._build_layout()
        self.pages = {}
        for key, _label, cls in self.PAGES:
            page = cls(self)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page
        self._center(1080, 780)
        self.minsize(940, 700)
        _dark_title_bar(self, self.dark)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Button-4>", lambda e: self._on_wheel(e, -1))
        self.bind_all("<Button-5>", lambda e: self._on_wheel(e, 1))
        self.show_page(start_page)
        self._tick_id = self.after(80, self._tick)

    # theme and styles
    def _apply_theme(self):
        dark = _windows_prefers_dark()
        themed = False
        try:
            import sv_ttk
            sv_ttk.set_theme("dark" if dark else "light")
            themed = True
        except Exception:
            style = ttk.Style(self)
            for name in ("vista", "clam"):
                if name in style.theme_names():
                    style.theme_use(name)
                    break
            dark = False
        families = set(tkfont.families(self))
        family = next((f for f in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
                                   "Helvetica Neue", "DejaVu Sans") if f in families), "TkDefaultFont")
        self.family = family
        colors = PALETTES["dark" if dark else "light"]
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=(family, 20, "bold"))
        style.configure("Heading.TLabel", font=(family, 11, "bold"))
        style.configure("ResultTitle.TLabel", font=(family, 12, "bold"))
        style.configure("Muted.TLabel", foreground=colors["muted"], font=(family, 10))
        style.configure("Danger.TLabel", foreground=colors["danger"], font=(family, 11, "bold"))
        style.configure("Success.TLabel", foreground=colors["success"], font=(family, 12, "bold"))
        style.configure("Warning.TLabel", foreground=colors["warning"], font=(family, 12, "bold"))
        style.configure("Banner.TLabel", background=colors["banner"], foreground=colors["text"],
                        font=(family, 10))
        style.configure("Status.TLabel", foreground=colors["muted"], font=(family, 10))
        if not themed:
            style.configure("Accent.TButton", font=(family, 10, "bold"))
        return dark, themed

    def style_listbox(self, listbox):
        c = self.colors
        listbox.configure(background=c["log_bg"], foreground=c["text"], selectbackground=c["accent"],
                          selectforeground="#000000" if self.dark else "#ffffff",
                          highlightbackground=c["border"], highlightcolor=c["accent"],
                          font=(self.family, 10))

    # layout
    def _build_layout(self):
        c = self.colors
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        sidebar = tk.Frame(self, background=c["sidebar"], width=230)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        tk.Label(sidebar, text=APP_NAME, background=c["sidebar"], foreground=c["text"],
                 font=(self.family, 15, "bold"), anchor="w").pack(fill="x", padx=22, pady=(26, 0))
        tk.Label(sidebar, text=f"Version {__version__}", background=c["sidebar"],
                 foreground=c["muted"], font=(self.family, 9), anchor="w").pack(fill="x", padx=22)
        tk.Frame(sidebar, background=c["sidebar"], height=22).pack(fill="x")
        for key, label, _cls in self.PAGES:
            self._nav_item(sidebar, key, label)
        tk.Label(sidebar, text="Your chats never leave\nthis PC.", justify="left",
                 background=c["sidebar"], foreground=c["muted"], font=(self.family, 9),
                 anchor="w").pack(side="bottom", fill="x", padx=22, pady=20)

        main = ttk.Frame(self)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)
        self.content = ttk.Frame(main)
        self.content.grid(row=0, column=0, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

        ttk.Separator(main).grid(row=1, column=0, sticky="ew")
        status = ttk.Frame(main, padding=(36, 10, 36, 12))
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(1, weight=1)
        ttk.Label(status, textvariable=self.status_text, style="Status.TLabel",
                  width=36).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=1, sticky="ew", padx=12)
        self.progress.grid_remove()
        self.cancel_button = ttk.Button(status, text="Cancel", command=self.runner.cancel)
        self.cancel_button.grid(row=0, column=2, padx=(0, 8))
        self.cancel_button.grid_remove()
        self.details_button = ttk.Button(status, text="Show details", command=self.toggle_details)
        self.details_button.grid(row=0, column=3)

        self.details = ttk.Frame(main, padding=(36, 0, 36, 14))
        self.log_text = tk.Text(self.details, height=9, wrap="word", borderwidth=0,
                                background=c["log_bg"], foreground=c["log_fg"],
                                insertbackground=c["text"], font=("Consolas", 9),
                                padx=10, pady=8, state="disabled")
        scroll = ttk.Scrollbar(self.details, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _nav_item(self, parent, key, label):
        c = self.colors
        row = tk.Frame(parent, background=c["sidebar"], cursor="hand2")
        row.pack(fill="x", padx=10, pady=1)
        bar = tk.Frame(row, width=3, background=c["sidebar"])
        bar.pack(side="left", fill="y", pady=8)
        text = tk.Label(row, text=label, background=c["sidebar"], foreground=c["text"],
                        font=(self.family, 10), anchor="w", padx=12, pady=9)
        text.pack(side="left", fill="x", expand=True)
        self._nav[key] = (row, bar, text)

        def enter(_e):
            if self.current != key:
                for w in (row, text):
                    w.configure(background=c["hover"])

        def leave(_e):
            if self.current != key:
                for w in (row, text):
                    w.configure(background=c["sidebar"])
        for widget in (row, text, bar):
            widget.bind("<Button-1>", lambda _e: self.show_page(key))
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)

    def show_page(self, key):
        c = self.colors
        self.current = key
        for name, (row, bar, text) in self._nav.items():
            selected = name == key
            background = c["selected"] if selected else c["sidebar"]
            row.configure(background=background)
            text.configure(background=background,
                           font=(self.family, 10, "bold" if selected else "normal"))
            bar.configure(background=c["accent"] if selected else background)
        page = self.pages[key]
        page.tkraise()
        page.on_show()

    def _center(self, width, height):
        scale = self.winfo_fpixels("1i") / 96.0
        width, height = int(width * scale), int(height * scale)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = min(width, sw - 40), min(height, sh - 80)
        self.geometry(f"{width}x{height}+{(sw - width) // 2}+{max((sh - height) // 2 - 20, 0)}")

    # task plumbing
    def register_action(self, *buttons):
        self._actions.extend(buttons)

    def call_soon(self, func):
        self.runner.events.put(("call", func))

    def set_busy(self, busy, title=""):
        for button in self._actions:
            try:
                button.configure(state="disabled" if busy else "normal")
            except tk.TclError:
                pass
        if busy:
            self.status_text.set(title + "…")
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.grid()
            self.progress.start(12)
            self.cancel_button.grid()
        else:
            self.progress.stop()
            self.progress.grid_remove()
            self.cancel_button.grid_remove()
            self.status_text.set("Ready")
            backup_page = self.pages.get("backup") if hasattr(self, "pages") else None
            if backup_page is not None and backup_page.plan is None:
                backup_page.start_button.configure(state="disabled")

    def show_progress(self, done, total, label):
        if total:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(value=int(1000 * min(done, total) / total))
            self.status_text.set(f"{label}  ·  {done:,} / {total:,}")
        else:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            self.status_text.set(label + "…")
        can_cancel = label not in self.runner.uncancellable
        self.cancel_button.configure(state="normal" if can_cancel else "disabled")

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        if float(self.log_text.index("end")) > 5000:
            self.log_text.delete("1.0", "1000.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def toggle_details(self):
        self._details_visible = not self._details_visible
        if self._details_visible:
            self.details.grid(row=3, column=0, sticky="ew")
            self.details_button.configure(text="Hide details")
        else:
            self.details.grid_remove()
            self.details_button.configure(text="Show details")

    def _on_wheel(self, event, units=None):
        widget = event.widget
        if isinstance(widget, (tk.Text, tk.Listbox)) or "combobox" in str(widget).lower():
            return
        if units is None:
            units = -1 if event.delta > 0 else 1
        self.pages[self.current].scroll(units * 3)

    def _tick(self):
        self.runner.poll()
        self._tick_id = self.after(80, self._tick)

    def destroy(self):
        try:
            self.after_cancel(self._tick_id)
        except Exception:
            pass
        super().destroy()

    def _on_close(self):
        if self.runner.busy:
            if self.runner.label in self.runner.uncancellable:
                messagebox.showwarning(APP_NAME, "Please wait: files are being written right "
                                       "now and stopping could leave them incomplete.")
                return
            if not messagebox.askyesno(APP_NAME, "A task is still running. Stop it and close?",
                                       default="no"):
                return
            self.runner.cancel()
        self.destroy()


def main():
    if sys.stdout is None:  # windowed executable has no console
        sys.stdout = open(os.devnull, "w")
        sys.stderr = sys.stdout
    try:
        app = App()
    except tk.TclError as error:
        print(f"Cannot open a window ({error}). Use the command line instead: --help")
        return 1
    app.mainloop()
    return 0
