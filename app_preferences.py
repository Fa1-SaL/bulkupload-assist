"""Settings UI, atomic non-secret preferences, and optional OS credential storage.

The JSON schema is deliberately allowlisted. API keys never enter that file.
The Settings window owns a draft; the application owns persistence and services.
"""
from copy import deepcopy
import importlib
import json
import math
import os
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from ui_interactions import animate_window_close, animate_window_open, touchpad_deltas


DEFAULT_PREFERENCES = {
    "text_scale": 1.0,
    "remember_workspace": True,
    "reopen_last_folder": True,
    "last_folder": "",
    "window_geometry": "",
    "column_widths": {},
    "sort_state": None,
    "remember_api_key": False,
    "model": "gpt-6-luna",
    "drive_folder_id": "",
    "recursive": False,
    "jd_matching_default": True,
    "remember_export_folder": True,
    "export_directory": "",
    "warn_previous_exports": True,
    "export_missing": False,
    "regular_export": False,
    "smooth_scrolling": True,
    "reduce_motion": False,
}


def normalize_preferences(values):
    """Return an independent, known-schema dictionary from untrusted JSON."""
    result = deepcopy(DEFAULT_PREFERENCES)
    if not isinstance(values, dict):
        return result
    for key, default in DEFAULT_PREFERENCES.items():
        value = values.get(key, default)
        if isinstance(default, bool):
            if isinstance(value, bool):
                result[key] = value
        elif isinstance(default, str):
            if isinstance(value, str):
                result[key] = value.strip()
    # Fixed application behavior; discard old user-selectable display overrides.
    result.update(text_scale=1.0, smooth_scrolling=True, reduce_motion=False)
    if not result["model"]:
        result["model"] = DEFAULT_PREFERENCES["model"]
    widths = values.get("column_widths")
    if isinstance(widths, dict):
        result["column_widths"] = {
            key: value for key, value in widths.items()
            if isinstance(key, str) and isinstance(value, int)
            and not isinstance(value, bool) and 20 <= value <= 10000
        }
    sort = values.get("sort_state")
    if (isinstance(sort, (list, tuple)) and len(sort) == 3
            and isinstance(sort[0], str) and isinstance(sort[1], bool)
            and isinstance(sort[2], bool)):
        result["sort_state"] = list(sort)
    return result


class PreferenceStore:
    """Read tolerant JSON and replace it atomically on the same filesystem."""
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return normalize_preferences(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, UnicodeError):
            return deepcopy(DEFAULT_PREFERENCES)

    def save(self, values):
        normalized = normalize_preferences(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(normalized, output, indent=2, ensure_ascii=False, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return normalized


class SecureStorageUnavailable(RuntimeError):
    """The optional OS keyring is missing, locked, or unavailable."""


class SecureApiKeyStore:
    """Lazy keyring access with no file, environment, or plaintext fallback."""
    def __init__(self, service_name="ResumeExtractor", username="api_key"):
        self.service_name = service_name
        self.username = username

    def _call(self, operation, *args):
        try:
            keyring = importlib.import_module("keyring")
            backend = keyring.get_keyring()
            # keyring can be configured to use plaintext third-party backends.
            # Only native OS credential stores are accepted, including for reads.
            secure_modules = {
                "keyring.backends.macOS", "keyring.backends.Windows",
                "keyring.backends.SecretService", "keyring.backends.kwallet",
                "keyring.backends.libsecret",
            }
            if type(backend).__module__ not in secure_modules:
                raise RuntimeError("The selected keyring is not a native secure credential store")
            return getattr(backend, operation)(self.service_name, self.username, *args)
        except Exception as exc:
            raise SecureStorageUnavailable(
                "Secure API key storage is unavailable. Install keyring and unlock your "
                "system Keychain/credential store, or leave ‘Remember API key’ off."
            ) from exc

    def get(self):
        return self._call("get_password")

    def set(self, api_key):
        self._call("set_password", api_key)

    def delete(self):
        # A missing credential is already the desired state.
        if self.get() is not None:
            self._call("delete_password")


class SettingsWindow(ctk.CTkToplevel):
    """One scrolling page of four sections; a transactional editor whose
    callbacks supply all service actions.

    on_save(preferences, api_key) runs only when Save is pressed. Test callbacks
    accept (api_key, model), and may return a status string.
    Clear callbacks run after a successful Save if requested in this draft.
    """
    BG = "#20242B"
    PANEL = "#292E37"
    TEXT = "#E6EAF0"
    MUTED = "#B8C0CC"
    SECONDARY = "#343A43"
    ACCENT = "#1F6AA5"

    def __init__(self, parent, current, api_key, models, on_save, *,
                 on_test_ai=None, on_clear_analysis=None, on_clear_history=None):
        super().__init__(parent, fg_color=self.BG)
        self.withdraw()
        self.title("Settings")
        self.geometry("600x640")
        self.minsize(480, 420)
        self.resizable(True, True)
        self.transient(parent)
        self._original = normalize_preferences(current)
        self._original_key = str(api_key or "")
        self._on_save = on_save
        self._on_test_ai = on_test_ai
        self._on_clear_analysis = on_clear_analysis
        self._on_clear_history = on_clear_history
        self._pending_clears = set()
        self._busy = False
        self._connection_widgets = []
        self.variables = {}
        for key, value in self._original.items():
            if key in ("text_scale", "smooth_scrolling", "reduce_motion", "drive_folder_id"):
                continue
            if isinstance(value, bool):
                self.variables[key] = tk.BooleanVar(self, value=value)
            elif isinstance(value, str):
                self.variables[key] = tk.StringVar(self, value=value)
        self._key_var = tk.StringVar(self, value=self._original_key)
        self._status = tk.StringVar(self, value="Changes apply when you save.")
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda event: self.cancel())
        self._wheel_remainder = 0.0
        # Wheel events can target the focused Toplevel rather than the hovered
        # label/canvas. Handle them once here, before CTk's global handlers.
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.bind(sequence, self._scroll_settings, add='+')
        # Tk 9 delivers trackpad gestures as <TouchpadScroll>, which nothing else handles here.
        self.bind('<TouchpadScroll>', self._touchpad_scroll, add='+')
        self._closing = False
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        # A handful of settings reads best as one page; sections replace tabs.
        self.page = ctk.CTkScrollableFrame(self, fg_color=self.BG, corner_radius=0)
        self.page.grid(row=0, column=0, sticky="nsew", padx=(10, 8), pady=(8, 4))
        self.page.grid_columnconfigure(0, weight=1)
        self.page._parent_canvas.configure(yscrollincrement=1)
        # CTk's built-in scrollbar wheel callback would otherwise scroll once
        # before this window's wheel handler sees the same event.
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.page._scrollbar._canvas.bind(sequence, self._scroll_settings)
        self._pages = {}
        for category in ("General", "Connections", "Processing", "Export"):
            section = ctk.CTkFrame(self.page, fg_color="transparent")
            section.pack(fill="x", pady=(0, 8))
            self._pages[category] = section
            self._title(section, category)
        self._build_general(self._pages["General"])
        self._build_connections(self._pages["Connections"], models)
        self._build_processing(self._pages["Processing"])
        self._build_export(self._pages["Export"])
        footer = ctk.CTkFrame(self, fg_color="#1B1F25", corner_radius=0)
        footer.grid(row=1, column=0, columnspan=2, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(footer, textvariable=self._status, text_color=self.MUTED,
                     anchor="w", wraplength=430, justify="left").grid(
                         row=0, column=0, sticky="ew", padx=18, pady=14)
        self._button(footer, "Cancel", self.cancel, width=88).grid(row=0, column=1, padx=6, pady=14)
        self.save_button = self._button(footer, "Save", self.save, width=88, primary=True)
        self.save_button.grid(row=0, column=2, padx=(6, 18), pady=14)
        footer.bind("<Configure>", lambda event: footer.grid_slaves(row=0, column=0)[0].configure(
            wraplength=max(180, event.width / footer._get_widget_scaling() - 250)))
        self._select_page("General")

    def _button(self, parent, text, command, width=140, primary=False):
        return ctk.CTkButton(parent, text=text, command=command, height=36, width=width,
            fg_color=self.ACCENT if primary else self.SECONDARY,
            hover_color="#185A8E" if primary else "#414955", text_color=self.TEXT)

    def _title(self, parent, text):
        ctk.CTkLabel(parent, text=text, text_color=self.TEXT, anchor="w",
            font=ctk.CTkFont(size=17, weight="bold")).pack(fill="x", padx=12, pady=(14, 6))

    def _description(self, parent, text):
        label = ctk.CTkLabel(parent, text=text, text_color=self.MUTED, justify="left",
                            anchor="w", wraplength=445)
        label.pack(fill="x", padx=12, pady=(0, 12))
        parent.bind("<Configure>", lambda event, widget=label:
                    widget.configure(wraplength=max(140,
                        event.width / parent._get_widget_scaling() - 34)), add="+")
        return label

    def _toggle(self, parent, key, title, description):
        switch = ctk.CTkSwitch(parent, text=title, variable=self.variables[key],
            text_color=self.TEXT, progress_color=self.ACCENT)
        switch.pack(fill="x", padx=12, pady=(10, 5))
        self._description(parent, description)
        return switch

    def _field(self, parent, key, label, placeholder=""):
        ctk.CTkLabel(parent, text=label, text_color=self.TEXT, anchor="w").pack(
            fill="x", padx=12, pady=(8, 4))
        entry = ctk.CTkEntry(parent, textvariable=self.variables[key], height=36,
            placeholder_text=placeholder, fg_color=self.PANEL, text_color=self.TEXT)
        entry.pack(fill="x", padx=12, pady=(0, 8))
        return entry

    def _build_general(self, page):
        self._toggle(page, "remember_workspace", "Remember workspace",
            "Keep window size, table column widths, and table sorting between launches.")
        self._toggle(page, "reopen_last_folder", "Reopen last resume folder",
            "Restore the folder you last selected when the application starts.")
    def _build_connections(self, page, models):
        # Shown only during a batch: why these controls are locked, next to them.
        self.connection_note = ctk.CTkLabel(page, text="Locked while a batch is running, so it keeps "
            "the connection it started with. Other settings can still be saved.",
            text_color="#E5B567", anchor="w", justify="left", wraplength=445)
        page.bind("<Configure>", lambda event: self.connection_note.configure(
            wraplength=max(140, event.width / page._get_widget_scaling() - 34)), add="+")
        ctk.CTkLabel(page, text="AI API key", anchor="w", text_color=self.TEXT).pack(fill="x", padx=12)
        self.api_entry = ctk.CTkEntry(page, textvariable=self._key_var, show="•", height=36,
            fg_color=self.PANEL, text_color=self.TEXT)
        self.api_entry.pack(fill="x", padx=12, pady=(5, 8))
        self._connection_widgets.append(self.api_entry)
        self._connection_widgets.append(self._toggle(page, "remember_api_key", "Remember API key",
            "Off by default. When enabled, save the key only in the system Keychain or credential store; "
            "otherwise keep it for this session only."))
        ctk.CTkLabel(page, text="AI model", anchor="w", text_color=self.TEXT).pack(fill="x", padx=12)
        choices = list(dict.fromkeys([str(model) for model in models] + [self._original["model"]]))
        self.model_menu = ctk.CTkOptionMenu(page, values=choices,
            variable=self.variables["model"], height=36, fg_color=self.SECONDARY,
            button_color=self.ACCENT, text_color=self.TEXT)
        self.model_menu.pack(fill="x", padx=12, pady=(5, 8))
        self._connection_widgets.append(self.model_menu)
        test_ai = self._button(page, "Test AI connection", self._test_ai)
        test_ai.pack(anchor="w", padx=12, pady=(0, 15))
        self._connection_widgets.append(test_ai)
    def _build_processing(self, page):
        self._toggle(page, "recursive", "Include subfolders",
            "Find supported resumes inside every subfolder of the selected folder.")
        self._toggle(page, "jd_matching_default", "Enable JD matching by default",
            "Start new batches with job-description matching enabled. You can change it for each batch.")
        self._description(page, "Saved analysis helps avoid processing the same resume again. "
            "Clearing it requires resumes to be analyzed again.")
        self._button(page, "Clear saved analysis", lambda: self._queue_clear("analysis"), width=180).pack(
            anchor="w", padx=12, pady=(0, 12))

    def _build_export(self, page):
        self._toggle(page, "remember_export_folder", "Remember export folder",
            "Use the selected export folder as the starting location for future CSV saves.")
        self.export_entry = self._field(page, "export_directory", "Export folder")
        self._button(page, "Choose folder…", self._choose_export_folder).pack(anchor="w", padx=12, pady=(0, 12))
        self._toggle(page, "warn_previous_exports", "Warn about previously exported resumes",
            "Show a warning before including resumes already recorded in export history.")
        self._toggle(page, "export_missing", "Export Missing too",
            "Off by default. Also include parsed resumes with incomplete candidate fields, "
            "leaving missing values blank. Unreadable or unparsed files are excluded.")
        self._toggle(page, "regular_export", "Regular export",
            "Off by default. Export candidate fields only. Match % is included when JD matching "
            "was enabled for the batch.")
        self._button(page, "Clear export history", lambda: self._queue_clear("history"), width=180).pack(
            anchor="w", padx=12, pady=(6, 12))

    def _select_page(self, category):
        """Scroll a section to the top of the page."""
        self._active_page = category
        self._wheel_remainder = 0.0
        self.update_idletasks()
        canvas = self.page._parent_canvas
        region = canvas.bbox("all")
        if region and region[3] > 0:
            canvas.yview_moveto(self._pages[category].winfo_y() / region[3])

    def _scroll_settings(self, event):
        """Scroll the active page even when native wheel focus is the window."""
        try:
            canvas = self.page._parent_canvas
            if canvas.yview() == (0.0, 1.0):
                return 'break'
            button = getattr(event, 'num', None)
            if button in (4, 5):
                pixels = -48 if button == 4 else 48
            else:
                delta = float(getattr(event, 'delta', 0))
                if not math.isfinite(delta): return 'break'
                system = self.tk.call('tk', 'windowingsystem')
                factor = 48 / 120 if system == 'win32' or abs(delta) >= 120 else 16
                pixels = -delta * factor
            total = self._wheel_remainder + pixels
            whole = math.trunc(total)
            self._wheel_remainder = total - whole
            if whole: canvas.yview_scroll(whole, 'units')
        except (tk.TclError, ValueError, TypeError, KeyError):
            pass
        return 'break'

    def _touchpad_scroll(self, event):
        """Follow the fingers 1:1 in pixels; the trackpad already supplies momentum."""
        _dx, dy = touchpad_deltas(self, event)
        try:
            canvas = self.page._parent_canvas
            if dy and canvas.yview() != (0.0, 1.0):
                total = self._wheel_remainder - dy
                whole = math.trunc(total)
                self._wheel_remainder = total - whole
                if whole: canvas.yview_scroll(whole, 'units')
        except (tk.TclError, ValueError):
            pass
        return 'break'

    def _choose_export_folder(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.variables["export_directory"].get() or None,
                                         title="Choose export folder")
        if folder:
            self.variables["export_directory"].set(folder)

    def _queue_clear(self, kind):
        self._pending_clears.add(kind)
        self._status.set(f"Saved {kind} will be cleared when you save. Cancel keeps it.")

    def _test(self, callback, *args):
        if not callback:
            self._status.set("Connection test is unavailable in this session.")
            return
        self._status.set("Testing connection…")
        self.update_idletasks()
        try:
            result = callback(*args)
            self._status.set(str(result) if result is not None else "Connection test started.")
        except Exception:
            # Callback exceptions may include a request URL or credential; never echo them.
            self._status.set("Connection test failed. Check your credentials and access, then try again.")

    def _test_ai(self):
        self._test(self._on_test_ai, self._key_var.get().strip(), self.variables["model"].get())

    def get_draft(self):
        draft = deepcopy(self._original)
        draft.update({key: variable.get() for key, variable in self.variables.items()})
        return normalize_preferences(draft)

    def set_busy(self, busy):
        """Only connection inputs lock during a batch; everything else stays editable."""
        self._busy = bool(busy)
        for widget in self._connection_widgets:
            widget.configure(state="disabled" if busy else "normal")
        if busy:
            self.connection_note.pack(fill="x", padx=12, pady=(0, 10), after=self._pages["Connections"].winfo_children()[0])
        else:
            self.connection_note.pack_forget()

    def save(self):
        draft = self.get_draft()
        key = self._key_var.get().strip()
        try:
            result = self._on_save(deepcopy(draft), key)
            if result is False:
                return
        except Exception as exc:
            self._status.set(str(exc) if isinstance(exc, (SecureStorageUnavailable, ValueError))
                             else "Settings could not be saved. Please try again.")
            return
        self._original = deepcopy(draft)
        self._original_key = key
        clear_failures = False
        for kind, callback in (("analysis", self._on_clear_analysis), ("history", self._on_clear_history)):
            if kind in self._pending_clears and callback:
                try:
                    callback()
                except Exception:
                    clear_failures = True
        self._pending_clears.clear()
        if clear_failures:
            self._status.set("Settings saved. Some saved data could not be cleared.")
        else:
            self.hide()

    def cancel(self):
        for key, variable in self.variables.items():
            variable.set(self._original[key])
        self._key_var.set(self._original_key)
        self._pending_clears.clear()
        self.hide()

    def show(self, current=None, api_key=None):
        """Open a fresh draft, optionally using updated application state."""
        if current is not None:
            self._original = normalize_preferences(current)
        if api_key is not None:
            self._original_key = str(api_key)
        for key, variable in self.variables.items():
            variable.set(self._original[key])
        self._key_var.set(self._original_key)
        self._pending_clears.clear()
        self._status.set("Changes apply when you save.")
        self._closing = False
        animate_window_open(self)
        self.lift()
        self.focus_set()

    def is_open(self):
        """Shown and not on its way out; a closing fade still counts as closed."""
        return bool(self.winfo_viewable()) and not self._closing

    def hide(self):
        if self._closing or not self.winfo_viewable():
            self._closing = False
            self.withdraw()
            return
        self._closing = True
        animate_window_close(self, self._finish_hide)

    def _finish_hide(self):
        self._closing = False
        self.withdraw()
