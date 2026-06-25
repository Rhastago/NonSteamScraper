import tkinter as tk
from tkinter import ttk, messagebox
import threading
import requests
import os
import sys
import glob
import webbrowser
from PIL import Image, ImageTk
from find_games import (
    get_non_steam_games, search_game, download_all_artwork,
    GRID_FOLDER, load_api_key, save_api_key, verify_api_key,
    add_to_skip_list, SKIP_FILE, save_name_override, get_cache_size,
    clear_cache, clean_old_cache,
    is_steam_running, restart_steam, get_all_steam_users,
    clear_managed_artwork, register_managed_file, set_shortcut_icon,
    load_prefs, save_prefs, DEFAULT_PREFS, STEAM_NOT_FOUND, full_reset,
    search_sgdb_autocomplete, clear_slot_files, download_apng
)

VERSION = "1.1.0"

FIRST_RUN_FILE  = os.path.expanduser("~/.steamart_firstrun")
THEME_FILE      = os.path.expanduser("~/.steamart_theme")
GEOMETRY_FILE   = os.path.expanduser("~/.steamart_geometry")


def resource_path(relative):
    """Return the absolute path to a bundled resource, working both when running
    as a script and when packaged by PyInstaller (which unpacks to sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)

# Color palettes for light and dark appearance modes.
THEMES = {
    "light": {
        "bg": "#f0f0f0", "fg": "#1a1a1a", "entry_bg": "#ffffff",
        "select_bg": "#d8d8d8", "muted": "#888888", "muted2": "#666666",
        "placeholder": "#cccccc", "link": "#1565c0", "warn": "#d17a00",
        "danger": "#c0392b", "ok": "#2e7d32", "button_bg": "#e4e4e4",
        "button_fg": "#1a1a1a",
    },
    "dark": {
        "bg": "#2b2b2b", "fg": "#e6e6e6", "entry_bg": "#3c3f41",
        "select_bg": "#4a4d4f", "muted": "#9aa0a6", "muted2": "#888888",
        "placeholder": "#555555", "link": "#5aa0ff", "warn": "#ffb14e",
        "danger": "#ff6b6b", "ok": "#66bb6a", "button_bg": "#3c3f41",
        "button_fg": "#e6e6e6",
    },
}


def load_theme():
    """Return the saved theme name, defaulting to dark."""
    try:
        with open(THEME_FILE, "r") as f:
            name = f.read().strip()
            return name if name in THEMES else "dark"
    except Exception:
        return "dark"


def save_theme(name):
    """Persist the chosen theme name."""
    with open(THEME_FILE, "w") as f:
        f.write(name)


class SteamArtApp:
    def __init__(self, window):
        if STEAM_NOT_FOUND:
            messagebox.showerror(
                "Steam Not Found",
                "Could not find Steam user data.\n\n"
                "Make sure Steam is installed and you have logged in at least once,\n"
                "then restart NonSteamScraper.",
                parent=window
            )
            sys.exit(1)
        self.window = window
        self.theme_name = load_theme()
        self.theme = THEMES[self.theme_name]
        self.window.title(f"NonSteamScraper v{VERSION}")
        self.window.minsize(560, 480)
        self.window.resizable(True, True)
        self.window.config(bg=self.theme["bg"])
        self._set_window_icon()
        self.games = []
        self.hidden_visible = False
        self.hidden_frame = None
        self.selected_row = None
        self.selected_btn = None
        self.selected_rename_btn = None
        self.last_fetch_files = []
        self.build_ui()
        clean_old_cache()
        self.load_games()
        self.check_steam_running()
        self._autosize_window()
        self._restore_geometry()
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.check_first_run()

    def _set_window_icon(self):
        """Load the bundled icon for the window and all child windows.
        Silently does nothing if the icon file is missing."""
        try:
            self._icon_image = ImageTk.PhotoImage(Image.open(resource_path("icon.png")))
            self.window.iconphoto(True, self._icon_image)
        except Exception:
            pass

    def _autosize_window(self):
        """Size the window to fit all its content so nothing is clipped on open.
        Temporarily packs the progress widgets to include them in the measurement,
        then hides them again so they only appear during an active fetch."""
        self.progress_label.pack(pady=2)
        self.progress_bar.pack(fill="x", padx=20, pady=2)
        self.window.update_idletasks()
        req_w = max(self.window.winfo_reqwidth(), 700)
        req_h = self.window.winfo_reqheight()
        self.progress_label.pack_forget()
        self.progress_bar.pack_forget()
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        x = (screen_w // 2) - (req_w // 2)
        y = (screen_h // 2) - (req_h // 2)
        self.window.geometry(f"{req_w}x{req_h}+{max(x, 0)}+{max(y, 0)}")

    # --- Themed widget helpers ---

    def _btn(self, parent, text, command, **kw):
        """Create a button styled for the current theme."""
        return tk.Button(parent, text=text, command=command,
                         bg=self.theme["button_bg"], fg=self.theme["button_fg"],
                         activebackground=self.theme["select_bg"],
                         activeforeground=self.theme["fg"], **kw)

    def _flat_btn(self, parent, text, command, **kw):
        """Create a borderless button that blends into the background."""
        return tk.Button(parent, text=text, command=command, relief="flat",
                         bg=self.theme["bg"], fg=self.theme["fg"],
                         activebackground=self.theme["bg"],
                         activeforeground=self.theme["fg"], **kw)

    def _frame(self, parent, **kw):
        return tk.Frame(parent, bg=self.theme["bg"], **kw)

    def _label(self, parent, text, **kw):
        kw.setdefault("bg", self.theme["bg"])
        kw.setdefault("fg", self.theme["fg"])
        return tk.Label(parent, text=text, **kw)

    def _make_scrollable_frame(self, parent, padx=10, pady=0):
        """Return a (canvas, body_frame) pair inside a scrollable container."""
        t = self.theme
        container = tk.Frame(parent, bg=t["bg"])
        container.pack(fill="both", expand=True, padx=padx, pady=pady)
        canvas = tk.Canvas(container, bg=t["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=t["bg"])
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return canvas, body

    def _add_tooltip(self, widget, text):
        """Show a small tooltip near the cursor when hovering over widget."""
        tip = None

        def show(event):
            nonlocal tip
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 6}")
            tk.Label(tip, text=text, font=("Arial", 9),
                     bg="#ffffe0", fg="#1a1a1a", relief="solid", borderwidth=1,
                     padx=4, pady=2).pack()

        def hide(event):
            nonlocal tip
            if tip:
                tip.destroy()
                tip = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def _restore_geometry(self):
        try:
            if os.path.exists(GEOMETRY_FILE):
                with open(GEOMETRY_FILE, "r") as f:
                    geo = f.read().strip()
                if "+" in geo:
                    pos = "+" + "+".join(geo.split("+")[1:])
                    self.window.geometry(pos)
        except Exception:
            pass

    def _on_close(self):
        try:
            with open(GEOMETRY_FILE, "w") as f:
                f.write(self.window.geometry())
        except Exception:
            pass
        self.window.destroy()

    def relaunch_app(self):
        """Restart the application so a theme change takes effect.
        Works for both the script and the packaged executable."""
        try:
            self.window.destroy()
        except Exception:
            pass
        if getattr(sys, "frozen", False):
            os.execv(sys.executable, [sys.executable])
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    def toggle_theme(self):
        """Switch between light and dark mode and relaunch."""
        save_theme("dark" if self.theme_name == "light" else "light")
        self.relaunch_app()

    def check_first_run(self):
        """Show the quick-start guide automatically on the first launch."""
        if not os.path.exists(FIRST_RUN_FILE):
            with open(FIRST_RUN_FILE, "w") as f:
                f.write("done")
            self.window.after(300, self.show_info)

    def build_ui(self):
        """Construct all main window UI elements."""
        t = self.theme

        header_frame = self._frame(self.window)
        header_frame.pack(fill="x", padx=20, pady=10)
        self._label(header_frame, "NonSteamScraper", font=("Arial", 20, "bold")).pack(side="left")
        reload_btn = self._flat_btn(header_frame, "🔄", self.refresh_library, font=("Arial", 12))
        reload_btn.pack(side="left", padx=6)
        self._add_tooltip(reload_btn, "Reload")
        art_btn = self._flat_btn(header_frame, "🎨", self.open_art_prefs, font=("Arial", 12))
        art_btn.pack(side="left", padx=2)
        self._add_tooltip(art_btn, "Art Style Preferences")
        settings_btn = self._flat_btn(header_frame, "⚙", self.open_settings, font=("Arial", 14))
        settings_btn.pack(side="right")
        self._add_tooltip(settings_btn, "Settings")
        info_btn = self._flat_btn(header_frame, "ℹ", self.show_info, font=("Arial", 14))
        info_btn.pack(side="right", padx=4)
        self._add_tooltip(info_btn, "Information")

        self.summary_label = self._label(self.window, "Loading...", font=("Arial", 11))
        self.summary_label.pack()

        list_container = self._frame(self.window)
        list_container.pack(fill="both", expand=True, padx=20, pady=10)

        self.list_canvas = tk.Canvas(list_container, bg=t["bg"], highlightthickness=0)
        list_scrollbar = tk.Scrollbar(list_container, orient="vertical", command=self.list_canvas.yview)
        self.list_frame = self._frame(self.list_canvas)

        self.list_frame.bind("<Configure>", lambda e: self.list_canvas.configure(
            scrollregion=self.list_canvas.bbox("all")))
        self._list_window = self.list_canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        # Keep the inner frame width synced to the canvas so rows expand on resize
        self.list_canvas.bind("<Configure>", lambda e: self.list_canvas.itemconfig(
            self._list_window, width=e.width))
        self.list_canvas.configure(yscrollcommand=list_scrollbar.set)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        list_scrollbar.pack(side="right", fill="y")

        # Progress bar — hidden until a fetch is in progress
        self.progress_var = tk.DoubleVar()
        self.progress_label = self._label(self.window, "", font=("Arial", 9))
        self.progress_bar = ttk.Progressbar(self.window, variable=self.progress_var, maximum=100)

        self.fetch_button = self._btn(self.window, "Fetch Missing Artwork",
                                       self.start_fetch, font=("Arial", 13, "bold"))
        self.fetch_button.pack(pady=10)

        # Undo button — enabled only after a successful fetch
        self.undo_button = self._btn(self.window, "↩️ Undo Last Fetch",
                                     self.undo_fetch, font=("Arial", 10))
        self.undo_button.config(state="disabled")
        self.undo_button.pack()

        log_frame = self._frame(self.window)
        log_frame.pack(fill="both", padx=20, pady=5)
        log_scrollbar = tk.Scrollbar(log_frame)
        log_scrollbar.pack(side="right", fill="y")
        self.log_box = tk.Text(log_frame, height=7, font=("Courier", 10), state="disabled",
                                yscrollcommand=log_scrollbar.set, bg=t["entry_bg"],
                                fg=t["fg"], insertbackground=t["fg"],
                                highlightthickness=0, relief="flat")
        self.log_box.pack(fill="both", expand=True)
        log_scrollbar.config(command=self.log_box.yview)

        self.status_bar = self._label(self.window, "Ready", font=("Arial", 9), anchor="w")
        self.status_bar.pack(fill="x", padx=20, pady=5)

    def check_steam_running(self):
        """Display a warning if Steam is open, since artwork changes require a restart."""
        if is_steam_running():
            self.status_bar.config(
                text="⚠️ Steam is running — restart Steam after fetching to see changes",
                fg=self.theme["warn"]
            )

    def _bind_wheel_to_canvas(self, widget, canvas):
        """Bind mouse-wheel scrolling on a widget and all of its descendants so they
        scroll the given canvas. Uses direct per-widget bindings (never bind_all),
        so opening or closing other windows can no longer disturb scrolling."""
        def _on_wheel(event):
            num = getattr(event, "num", None)
            if num == 4:
                canvas.yview_scroll(-1, "units")
            elif num == 5:
                canvas.yview_scroll(1, "units")
            elif getattr(event, "delta", 0):
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(seq, _on_wheel)
        for child in widget.winfo_children():
            self._bind_wheel_to_canvas(child, canvas)

    def _remove_from_skip(self, app_id):
        """Remove a single app ID from the skip file."""
        if not os.path.exists(SKIP_FILE):
            return
        with open(SKIP_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(SKIP_FILE, "w", encoding="utf-8") as f:
            for line in lines:
                if line.strip() != str(app_id):
                    f.write(line)

    def refresh_library(self):
        """Reload the library and recheck Steam status."""
        self.log("🔄 Reloading library...")
        self.load_games()
        self.status_bar.config(text="Ready", fg=self.theme["fg"])
        self.check_steam_running()

    def load_games(self):
        """Reload the game list from Steam and refresh the UI."""
        self.games = get_non_steam_games()
        needs_art = [g for g in self.games if not g["has_art"]]
        skipped    = [g for g in self.games if g.get("skipped")]
        normal     = [g for g in self.games if not g.get("skipped")]

        self.selected_row = None
        self.selected_btn = None
        self.selected_rename_btn = None

        for widget in self.list_frame.winfo_children():
            widget.destroy()

        if not self.games:
            # No non-Steam games in the library — explain how to add some.
            self.summary_label.config(text="No non-Steam games found")
            self._label(
                self.list_frame,
                "No non-Steam games were found in your Steam library.\n\n"
                "Add one in Steam first:\n"
                "Games -> Add a Non-Steam Game to My Library,\n"
                "then click the refresh button above to reload.",
                font=("Arial", 11), justify="left", anchor="w"
            ).pack(fill="x", padx=10, pady=20)
            self.fetch_button.config(state="disabled", text="Nothing to fetch")
            self.window.update_idletasks()
            content_h = self.list_frame.winfo_reqheight()
            screen_h = self.window.winfo_screenheight()
            self.list_canvas.config(height=min(max(content_h, 40), screen_h // 3))
            self._bind_wheel_to_canvas(self.list_canvas, self.list_canvas)
            return

        self.summary_label.config(
            text=f"Found {len(self.games)} Non-Steam Games — {len(needs_art)} need artwork")

        for game in normal:
            self.build_game_row(game)

        if skipped:
            self.build_hidden_section(skipped)

        self.fetch_button.config(
            state="disabled" if not needs_art else "normal",
            text="Nothing to fetch" if not needs_art else "Fetch Missing Artwork"
        )

        # Re-attach wheel scrolling to the freshly built rows.
        self.window.update_idletasks()
        content_h = self.list_frame.winfo_reqheight()
        screen_h = self.window.winfo_screenheight()
        self.list_canvas.config(height=min(content_h, screen_h // 3))
        self._bind_wheel_to_canvas(self.list_canvas, self.list_canvas)

    def build_game_row(self, game):
        """Build a single game row with click-to-reveal action buttons."""
        t = self.theme
        icon = "✅" if game["has_art"] else "🎨"
        row = tk.Frame(self.list_frame, pady=2, cursor="hand2", bg=t["bg"])
        row.pack(fill="x", padx=5)

        label = tk.Label(row, text=f"{icon}  {game['name'][:40]}",
                          font=("Courier", 11), anchor="w", cursor="hand2",
                          bg=t["bg"], fg=t["fg"])
        label.pack(side="left", fill="x", expand=True)

        refetch_btn = self._btn(row, "Re-fetch", lambda g=game: self.refetch_game(g), font=("Arial", 8))
        rename_btn  = self._btn(row, "Search", lambda g=game: self.open_sgdb_search(g), font=("Arial", 8))

        def on_click(e, r=row, btn=refetch_btn, rbtn=rename_btn):
            # Deselect any previously selected row
            if self.selected_btn and self.selected_btn != btn:
                self.selected_btn.pack_forget()
                if self.selected_rename_btn:
                    self.selected_rename_btn.pack_forget()
                if self.selected_row:
                    self.selected_row.config(bg=t["bg"])
                    for child in self.selected_row.winfo_children():
                        child.config(bg=t["bg"])

            if self.selected_btn == btn:
                # Toggle off if already selected
                btn.pack_forget()
                rbtn.pack_forget()
                r.config(bg=t["bg"])
                for child in r.winfo_children():
                    child.config(bg=t["bg"])
                self.selected_row = self.selected_btn = self.selected_rename_btn = None
            else:
                # Select this row and show action buttons
                btn.pack(side="right", padx=2)
                rbtn.pack(side="right", padx=2)
                r.config(bg=t["select_bg"])
                for child in r.winfo_children():
                    child.config(bg=t["select_bg"])
                self.selected_row = r
                self.selected_btn = btn
                self.selected_rename_btn = rbtn

        row.bind("<Button-1>", on_click)
        label.bind("<Button-1>", on_click)

    def build_hidden_section(self, skipped_games):
        """Build the collapsible section for games that were permanently skipped."""
        self.toggle_btn = self._flat_btn(
            self.list_frame, f"▶  Hidden ({len(skipped_games)})",
            self.toggle_hidden, font=("Courier", 11), anchor="w")
        self.toggle_btn.pack(fill="x", padx=5, pady=4)
        self.hidden_frame = self._frame(self.list_frame)
        self.skipped_games = skipped_games
        self.hidden_visible = False
        for game in skipped_games:
            self.build_skipped_row(game)

    def build_skipped_row(self, game):
        """Build a row for a skipped game with reset and rename options."""
        t = self.theme
        row = tk.Frame(self.hidden_frame, pady=2, bg=t["bg"])
        row.pack(fill="x", padx=15)
        tk.Label(row, text=f"⏭  {game['name'][:35]}", font=("Courier", 11),
                  anchor="w", fg=t["muted"], bg=t["bg"]).pack(side="left", fill="x", expand=True)
        self._btn(row, "Search", lambda g=game: self.open_sgdb_search(g),
                  font=("Arial", 8)).pack(side="right", padx=2)
        self._btn(row, "Reset Skip", lambda g=game: self.reset_skip(g),
                  font=("Arial", 8)).pack(side="right", padx=2)

    def open_sgdb_search(self, game):
        """Open a live SGDB search dialog; on selection, save override and re-fetch."""
        t = self.theme
        dlg = tk.Toplevel(self.window)
        dlg.title("Find on SteamGridDB")
        dlg.minsize(420, 300)
        dlg.config(bg=t["bg"])
        dlg.transient(self.window)
        dlg.grab_set()

        # Centre over parent window
        self.window.update_idletasks()
        px = self.window.winfo_x() + self.window.winfo_width() // 2 - 210
        py = self.window.winfo_y() + self.window.winfo_height() // 2 - 180
        dlg.geometry(f"420x360+{px}+{py}")

        tk.Label(dlg, text=game["name"][:48], font=("Arial", 11, "bold"),
                 bg=t["bg"], fg=t["fg"]).pack(pady=(12, 2), padx=12)

        search_var = tk.StringVar(value=game["name"])
        entry = tk.Entry(dlg, textvariable=search_var, font=("Courier", 11),
                         bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"],
                         relief="flat", bd=4)
        entry.pack(fill="x", padx=12, pady=4)
        entry.select_range(0, "end")
        entry.focus_set()

        status_lbl = tk.Label(dlg, text="", font=("Arial", 9),
                               bg=t["bg"], fg=t["muted"])
        status_lbl.pack(padx=12, anchor="w")

        list_frame = tk.Frame(dlg, bg=t["bg"])
        list_frame.pack(fill="both", expand=True, padx=12, pady=4)
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        listbox = tk.Listbox(list_frame, font=("Courier", 10),
                              bg=t["entry_bg"], fg=t["fg"],
                              selectbackground=t["select_bg"],
                              selectforeground=t["fg"],
                              yscrollcommand=scrollbar.set,
                              highlightthickness=0, relief="flat",
                              activestyle="none")
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        btn_bar = tk.Frame(dlg, bg=t["bg"])
        btn_bar.pack(fill="x", padx=12, pady=(4, 10))

        results = []
        _after_id = [None]

        def _apply(evt=None):
            sel = listbox.curselection()
            if not sel:
                return
            chosen = results[sel[0]]
            save_name_override(game["app_id"], chosen["name"])
            self._remove_from_skip(game["app_id"])
            for f in glob.glob(os.path.join(GRID_FOLDER, f"{game['app_id']}*")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            self.log(f"🔍 Matched '{game['name']}' → '{chosen['name']}' (SGDB #{chosen['id']})")
            dlg.destroy()
            self.load_games()
            self.start_fetch()

        def _do_search(query):
            status_lbl.config(text="Searching…")
            dlg.update_idletasks()

            def worker():
                hits = search_sgdb_autocomplete(query)
                def update():
                    results.clear()
                    listbox.delete(0, "end")
                    if hits:
                        results.extend(hits)
                        for h in hits:
                            meta = "  ·  ".join(x for x in [h.get("year"), h.get("type")] if x)
                            listbox.insert("end", f"{h['name']}  ({meta})" if meta else h["name"])
                        status_lbl.config(text=f"{len(hits)} result(s)")
                    else:
                        status_lbl.config(text="No results found")
                try:
                    dlg.after(0, update)
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True).start()

        def _on_type(*_):
            if _after_id[0]:
                dlg.after_cancel(_after_id[0])
            q = search_var.get().strip()
            if len(q) < 2:
                status_lbl.config(text="")
                listbox.delete(0, "end")
                results.clear()
                return
            _after_id[0] = dlg.after(300, lambda: _do_search(q))

        def _entry_down(e):
            if results:
                listbox.focus_set()
                if not listbox.curselection():
                    listbox.selection_set(0)
                    listbox.activate(0)
                    listbox.see(0)
            return "break"

        def _listbox_up(e):
            sel = listbox.curselection()
            if sel and sel[0] == 0:
                listbox.selection_clear(0)
                entry.focus_set()
                return "break"

        search_var.trace_add("write", _on_type)
        listbox.bind("<Double-Button-1>", _apply)
        listbox.bind("<Return>", _apply)
        listbox.bind("<Escape>", lambda e: dlg.destroy())
        entry.bind("<Return>", lambda e: (listbox.selection_set(0), _apply()) if results else None)
        entry.bind("<Down>", _entry_down)
        entry.bind("<Escape>", lambda e: dlg.destroy())
        listbox.bind("<Up>", _listbox_up)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        self._btn(btn_bar, "Select", _apply).pack(side="right")
        self._btn(btn_bar, "Cancel", dlg.destroy).pack(side="right", padx=(0, 6))

        # Kick off initial search with the game's current name
        _do_search(game["name"])

    def toggle_hidden(self):
        """Expand or collapse the hidden games section."""
        if self.hidden_visible:
            self.hidden_frame.pack_forget()
            self.toggle_btn.config(text=f"▶  Hidden ({len(self.skipped_games)})")
            self.hidden_visible = False
        else:
            self.hidden_frame.pack(fill="x")
            self.toggle_btn.config(text=f"▼  Hidden ({len(self.skipped_games)})")
            self.hidden_visible = True

    def reset_skip(self, game):
        """Remove a game from the skip list so it will be retried on the next fetch."""
        self._remove_from_skip(game["app_id"])
        self.log(f"↩️ Reset skip for: {game['name']}")
        self.load_games()

    def refetch_game(self, game):
        """Clear existing artwork and skip data so a game is fully reprocessed."""
        self._remove_from_skip(game["app_id"])
        for f in glob.glob(os.path.join(GRID_FOLDER, f"{game['app_id']}*")):
            os.remove(f)
        self.log(f"🔄 Reset artwork for: {game['name']} — will re-fetch on next run")
        self.load_games()

    def undo_fetch(self):
        """Delete the files downloaded during the last fetch, reverting those games
        back to needing artwork. Works even on a first-time fetch with no prior state."""
        if not self.last_fetch_files:
            self.log("⚠️ Nothing to undo.")
            return
        removed = 0
        for path in self.last_fetch_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    removed += 1
            except Exception:
                pass
        self.last_fetch_files = []
        self.undo_button.config(state="disabled")
        self.log(f"↩️ Undo complete — removed {removed} file(s). Restart Steam to see changes.")
        self.load_games()

    def show_info(self):
        """Display a scrollable quick-start guide window."""
        t = self.theme
        info = tk.Toplevel(self.window)
        info.title("Quick Start Guide")
        info.geometry("520x500")
        info.minsize(420, 360)
        info.resizable(True, True)
        info.config(bg=t["bg"])
        info.update_idletasks()
        x = self.window.winfo_x() + (self.window.winfo_width() // 2) - 260
        y = self.window.winfo_y() + (self.window.winfo_height() // 2) - 250
        info.geometry(f"+{x}+{y}")

        tk.Label(info, text="Welcome to NonSteamScraper",
                  font=("Arial", 16, "bold"), bg=t["bg"], fg=t["fg"]).pack(pady=(12, 0))
        tk.Label(info, text=f"v{VERSION}",
                  font=("Arial", 10), bg=t["bg"], fg=t["muted"]).pack()

        def close_info():
            info.destroy()

        # Close button pinned at the bottom so it is always reachable
        self._btn(info, "Got it", close_info, font=("Arial", 11)).pack(side="bottom", pady=12)
        info.protocol("WM_DELETE_WINDOW", close_info)

        # Scrollable body
        canvas, body = self._make_scrollable_frame(info, padx=10)

        def section(title, lines):
            tk.Label(body, text=title, font=("Arial", 11, "bold"),
                      bg=t["bg"], fg=t["fg"], anchor="w").pack(fill="x", pady=(8, 0))
            for ln in lines:
                tk.Label(body, text=ln, font=("Arial", 10), bg=t["bg"], fg=t["fg"],
                          anchor="w", justify="left", wraplength=460).pack(fill="x")

        section("What it does", [
            "Automatically finds and applies cover, hero, and logo",
            "artwork for your non-Steam game shortcuts."
        ])
        section("1. Get a free API key", [
            "This app uses SteamGridDB for artwork. Create a free",
            "account, generate an API key, then paste it into Settings.",
            "Artwork cannot be fetched until a key is saved.",
        ])
        link = tk.Label(body, text="Open the SteamGridDB API key page",
                        font=("Arial", 10, "underline"), fg=t["link"],
                        bg=t["bg"], cursor="hand2", anchor="w")
        link.pack(fill="x")
        link.bind("<Button-1>", lambda e: webbrowser.open(
            "https://www.steamgriddb.com/profile/preferences/api"))
        section("2. Add your games to Steam", [
            "Add your non-Steam games as shortcuts in Steam first,",
            "then launch this app so it can detect them.",
        ])
        section("3. Fetch artwork", [
            "Click 'Fetch Missing Artwork'. Review the results and",
            "swap to alternatives you prefer. Restart Steam to see changes.",
        ])
        section("Tips", [
            "• If a game isn't found, use 'Rename' to set its correct title.",
            "• Use 'Re-fetch' to redo a game's artwork from scratch.",
            "• Skipped games are tucked into the 'Hidden' section.",
            "• Toggle dark/light mode anytime in Settings.",
            "• 'Undo Last Fetch' restores the previous artwork.",
        ])

        self._bind_wheel_to_canvas(canvas, canvas)

    def open_art_prefs(self):
        """Open the art style preferences window."""
        t = self.theme
        win = tk.Toplevel(self.window)
        win.title("Art Style Preferences")
        win.geometry("500x580")
        win.minsize(440, 420)
        win.resizable(True, True)
        win.config(bg=t["bg"])
        win.update_idletasks()
        x = self.window.winfo_x() + (self.window.winfo_width() // 2) - 250
        y = self.window.winfo_y() + (self.window.winfo_height() // 2) - 290
        win.geometry(f"+{x}+{y}")

        prefs = load_prefs()
        vars_ = {k: tk.StringVar(value=v) for k, v in prefs.items()}
        update_fns = {}

        def do_save():
            save_prefs({k: v.get() for k, v in vars_.items()})
            self.log("🎨 Art style preferences saved.")
            win.destroy()

        def do_reset():
            for k, v in DEFAULT_PREFS.items():
                if k in vars_:
                    vars_[k].set(v)
            for fn in update_fns.values():
                fn()

        warned = {"animated": False, "nsfw": False}

        def set_val(key, value):
            if key == "animated" and value in ("ok", "must_have") and not warned["animated"]:
                warned["animated"] = True
                if not messagebox.askyesno(
                    "Animated Art",
                    "Animated art (GIF/WebP) can be several MB per image.\n"
                    "Previews may take a while to load.\n\nContinue?",
                    parent=win
                ):
                    warned["animated"] = False
                    return
            if key == "nsfw" and value in ("ok", "must_have") and not warned["nsfw"]:
                warned["nsfw"] = True
                if not messagebox.askyesno(
                    "Explicit Content",
                    "Enabling NSFW artwork may display explicit or adult content.\n\n"
                    "Only enable this if you are of legal age in your country\n"
                    "and consent to viewing such material.\n\nContinue?",
                    parent=win
                ):
                    warned["nsfw"] = False
                    return
            vars_[key].set(value)
            if key in update_fns:
                update_fns[key]()

        header_row = tk.Frame(win, bg=t["bg"])
        header_row.pack(fill="x", padx=10, pady=(10, 0))
        self._btn(header_row, "↺ Reset to defaults", do_reset,
                  font=("Arial", 9)).pack(side="left")
        tk.Label(header_row, text="Art Style Preferences", font=("Arial", 16, "bold"),
                 bg=t["bg"], fg=t["fg"]).pack(side="left", expand=True)

        self._btn(win, "Save & Close", do_save, font=("Arial", 11)).pack(side="bottom", pady=12)
        win.protocol("WM_DELETE_WINDOW", do_save)

        canvas, body = self._make_scrollable_frame(win, padx=10)

        def pref_row(parent, label, key):
            var = vars_[key]
            row = tk.Frame(parent, bg=t["bg"])
            row.pack(fill="x", pady=2, padx=5)
            tk.Label(row, text=label, font=("Arial", 10), bg=t["bg"], fg=t["fg"],
                     width=18, anchor="w").pack(side="left")
            btns = {}
            for val, txt, col in [("must_have", "Always", t["ok"]),
                                   ("ok",        "OK",     t["warn"]),
                                   ("never",     "Never",  t["danger"])]:
                b = tk.Button(row, text=txt, font=("Arial", 8), width=9,
                              bg=t["button_bg"], fg=t["button_fg"],
                              activebackground=t["select_bg"], relief="groove",
                              command=lambda v=val, k=key: set_val(k, v))
                b.pack(side="left", padx=2)
                btns[val] = (b, col)

            def update(bmap=btns, v=var):
                cur = v.get()
                for val, (b, col) in bmap.items():
                    if val == cur:
                        b.config(bg=col, fg="#ffffff", relief="sunken")
                    else:
                        b.config(bg=t["button_bg"], fg=t["button_fg"], relief="groove")

            update_fns[key] = update
            update()

        def section(title):
            tk.Label(body, text=title, font=("Arial", 10, "bold"),
                     bg=t["bg"], fg=t["fg"], anchor="w").pack(fill="x", padx=5, pady=(10, 1))
            tk.Frame(body, bg=t["muted"], height=1).pack(fill="x", padx=5, pady=(0, 4))

        section("Global")
        pref_row(body, "Animated art  ", "animated")
        pref_row(body, "NSFW", "nsfw")
        pref_row(body, "Humor / Memes", "humor")
        section("Cover (Grid)")
        pref_row(body, "No logo overlay", "grid_no_logo")
        pref_row(body, "Alternate style", "grid_alternate")
        pref_row(body, "Blurred", "grid_blurred")
        pref_row(body, "Material design", "grid_material")
        section("Hero / Background")
        pref_row(body, "Alternate art", "hero_alternate")
        pref_row(body, "Blurred", "hero_blurred")
        section("Logo")
        pref_row(body, "Official", "logo_official")
        pref_row(body, "White style", "logo_white")
        pref_row(body, "Black style", "logo_black")
        section("Icon")
        pref_row(body, "Official", "icon_official")

        self._bind_wheel_to_canvas(canvas, canvas)

    def open_settings(self):
        """Open the settings panel."""
        t = self.theme
        settings = tk.Toplevel(self.window)
        settings.title("Settings")
        settings.geometry("480x520")
        settings.minsize(420, 360)
        settings.resizable(True, True)
        settings.config(bg=t["bg"])
        settings.update_idletasks()
        x = self.window.winfo_x() + (self.window.winfo_width() // 2) - 240
        y = self.window.winfo_y() + (self.window.winfo_height() // 2) - 260
        settings.geometry(f"+{x}+{y}")

        tk.Label(settings, text="Settings", font=("Arial", 16, "bold"),
                  bg=t["bg"], fg=t["fg"]).pack(pady=10)

        def close_settings():
            settings.destroy()
            self.window.lift()
            self.window.focus_force()

        # Close button pinned at the bottom so it is always reachable
        self._btn(settings, "Close", close_settings, font=("Arial", 11)).pack(side="bottom", pady=12)
        settings.protocol("WM_DELETE_WINDOW", close_settings)

        # Scrollable body
        canvas, body = self._make_scrollable_frame(settings, padx=5)

        # API Key
        api_frame = tk.LabelFrame(body, text="SteamGridDB API Key", padx=10, pady=8,
                                   bg=t["bg"], fg=t["fg"])
        api_frame.pack(fill="x", padx=15, pady=5)
        link_frame = tk.Frame(api_frame, bg=t["bg"])
        link_frame.pack(fill="x", pady=2)
        tk.Label(link_frame, text="Get your free key at ", font=("Arial", 9),
                  bg=t["bg"], fg=t["fg"]).pack(side="left")
        link = tk.Label(link_frame, text="SteamGridDB", font=("Arial", 9, "underline"),
                        fg=t["link"], bg=t["bg"], cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda e: webbrowser.open(
            "https://www.steamgriddb.com/profile/preferences/api"))
        key_row = tk.Frame(api_frame, bg=t["bg"])
        key_row.pack(fill="x", pady=4)
        api_var = tk.StringVar(value=load_api_key())
        api_entry = tk.Entry(key_row, textvariable=api_var, font=("Courier", 10), show="*",
                              width=24, bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"])
        api_entry.pack(side="left")
        self._btn(key_row, "👁",
                  lambda: api_entry.config(show="" if api_entry.cget("show") == "*" else "*"),
                  font=("Arial", 10)).pack(side="left", padx=2)

        # Status line shows whether a verified key is on file
        key_status = tk.Label(api_frame, text="", font=("Arial", 9), bg=t["bg"], anchor="w")
        key_status.pack(fill="x", pady=2)
        if load_api_key():
            key_status.config(text="✓ A valid API Key is saved", fg=t["ok"])

        def do_save_key():
            candidate = api_var.get().strip()
            if not candidate:
                key_status.config(text="✗ Enter a key first", fg=t["danger"])
                return
            key_status.config(text="Checking key…", fg=t["muted"])

            def worker():
                ok = verify_api_key(candidate)

                def finish():
                    try:
                        if ok:
                            save_api_key(candidate)
                            key_status.config(text="✓ Key verified and saved", fg=t["ok"])
                            self.log("🔑 API key verified and saved.")
                        else:
                            key_status.config(
                                text="✗ Key invalid or unreachable — not saved", fg=t["danger"])
                    except Exception:
                        pass
                self.window.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        self._btn(key_row, "Save", do_save_key, font=("Arial", 9)).pack(side="left", padx=2)

        # Appearance
        appearance_frame = tk.LabelFrame(body, text="Appearance", padx=10, pady=8,
                                          bg=t["bg"], fg=t["fg"])
        appearance_frame.pack(fill="x", padx=15, pady=5)
        tk.Label(appearance_frame, text=f"Current: {self.theme_name.capitalize()} mode",
                  font=("Arial", 10), bg=t["bg"], fg=t["fg"]).pack(side="left")
        toggle_text = "Switch to Dark Mode" if self.theme_name == "light" else "Switch to Light Mode"
        self._btn(appearance_frame, toggle_text, self.toggle_theme,
                  font=("Arial", 9)).pack(side="right")

        # Steam Account (only shown when multiple accounts are detected)
        users = get_all_steam_users()
        if len(users) > 1:
            account_frame = tk.LabelFrame(body, text="Steam Account", padx=10, pady=8,
                                           bg=t["bg"], fg=t["fg"])
            account_frame.pack(fill="x", padx=15, pady=5)
            tk.Label(account_frame, text="Select account:", font=("Arial", 10),
                      bg=t["bg"], fg=t["fg"]).pack(side="left")
            om = tk.OptionMenu(account_frame, tk.StringVar(value=users[0]), *users)
            om.config(bg=t["button_bg"], fg=t["button_fg"], highlightthickness=0)
            om.pack(side="left", padx=8)

        # Cache
        cache_frame = tk.LabelFrame(body, text="Cache", padx=10, pady=8,
                                     bg=t["bg"], fg=t["fg"])
        cache_frame.pack(fill="x", padx=15, pady=5)
        cache_label = tk.Label(cache_frame, text=f"Cache size: {get_cache_size()} MB",
                                font=("Arial", 10), bg=t["bg"], fg=t["fg"])
        cache_label.pack(side="left")

        def do_clear_cache():
            clear_cache()
            cache_label.config(text="Cache size: 0.0 MB")
            self.log("🗑️ Cache cleared.")

        self._btn(cache_frame, "Clear Cache", do_clear_cache, font=("Arial", 9)).pack(side="right")

        # Steam
        steam_frame = tk.LabelFrame(body, text="Steam", padx=10, pady=8,
                                     bg=t["bg"], fg=t["fg"])
        steam_frame.pack(fill="x", padx=15, pady=5)
        steam_status = "🟢 Running" if is_steam_running() else "⚫ Not running"
        tk.Label(steam_frame, text=f"Status: {steam_status}", font=("Arial", 10),
                  bg=t["bg"], fg=t["fg"]).pack(side="left")
        self._btn(steam_frame, "Restart Steam",
                  lambda: [restart_steam(), close_settings(), self.log("🔄 Steam restarting...")],
                  font=("Arial", 9)).pack(side="right")

        # Artwork
        artwork_frame = tk.LabelFrame(body, text="Artwork", padx=10, pady=8,
                                       bg=t["bg"], fg=t["fg"])
        artwork_frame.pack(fill="x", padx=15, pady=5)

        def do_clear_artwork():
            if messagebox.askyesno(
                "Clear All Artwork",
                "This will remove all artwork fetched by NonSteamScraper.\n\n"
                "Artwork you set manually in Steam will NOT be affected.\n\n"
                "Are you sure?",
                parent=settings
            ):
                deleted = clear_managed_artwork()
                self.log(f"🗑️ Cleared {deleted} artwork file(s) added by NonSteamScraper.")
                self.load_games()
                close_settings()

        tk.Button(artwork_frame, text="Clear All Artwork", font=("Arial", 9),
                   fg=t["danger"], bg=t["button_bg"], activebackground=t["select_bg"],
                   command=do_clear_artwork).pack(side="left")
        tk.Label(artwork_frame, text="Only removes artwork added by this app",
                  font=("Arial", 8), fg=t["muted2"], bg=t["bg"]).pack(side="left", padx=8)

        # Reset
        reset_frame = tk.LabelFrame(body, text="Reset", padx=10, pady=8,
                                     bg=t["bg"], fg=t["fg"])
        reset_frame.pack(fill="x", padx=15, pady=5)

        def do_full_reset():
            if messagebox.askyesno(
                "Reset App",
                "This will permanently delete:\n\n"
                "• Your API key\n"
                "• All artwork fetched by this app\n"
                "• All preferences and settings\n"
                "• The skip list and name overrides\n"
                "• The cache\n\n"
                "The app will restart as if it were the first launch.\n\n"
                "Are you sure?",
                parent=settings
            ):
                full_reset()
                self.relaunch_app()

        tk.Button(reset_frame, text="Reset App to Factory Defaults", font=("Arial", 9),
                   fg=t["danger"], bg=t["button_bg"], activebackground=t["select_bg"],
                   command=do_full_reset).pack(side="left")
        tk.Label(reset_frame, text="Cannot be undone",
                  font=("Arial", 8), fg=t["muted2"], bg=t["bg"]).pack(side="left", padx=8)

        self._bind_wheel_to_canvas(canvas, canvas)

        # Auto-size to fit all content, capped at screen height
        settings.update_idletasks()
        body_h  = body.winfo_reqheight()
        overhead = 110  # title label + close button + padding
        win_h   = max(360, min(body_h + overhead, settings.winfo_screenheight() - 80))
        new_y   = max(20, self.window.winfo_y() + self.window.winfo_height() // 2 - win_h // 2)
        settings.geometry(f"480x{win_h}+{x}+{new_y}")

    def _ui(self, fn):
        """Schedule fn to run on the main (UI) thread. Safe to call from workers."""
        self.window.after(0, fn)

    def log(self, message):
        """Append a message to the activity log. Thread-safe: the text-box mutation
        is marshaled onto the main thread, so this may be called from worker threads."""
        def append():
            self.log_box.config(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
            self.window.update_idletasks()
        self.window.after(0, append)

    def start_fetch(self):
        """Disable the fetch button, show progress UI, and start the fetch thread."""
        if not load_api_key():
            messagebox.showinfo(
                "API Key Required",
                "You need a free SteamGridDB API key before fetching artwork.\n\n"
                "Open Settings to add your key, or click the info (ℹ) button for help.",
                parent=self.window
            )
            self.open_settings()
            return

        self.fetch_button.config(state="disabled", text="Fetching...")
        self.undo_button.config(state="disabled")
        self.status_bar.config(text="Working...", fg=self.theme["fg"])
        self.progress_var.set(0)
        self.progress_label.pack(pady=2)
        self.progress_bar.pack(fill="x", padx=20, pady=2)
        thread = threading.Thread(target=self.run_fetch)
        thread.daemon = True
        thread.start()

    def run_fetch(self):
        """Background thread: search and download artwork for all games missing it."""
        self.last_fetch_files = []
        prefs = load_prefs()
        needs_art = [g for g in self.games if not g["has_art"]]
        if not needs_art:
            self.log("✅ All games already have artwork!")
            self._ui(lambda: self.fetch_button.config(state="disabled", text="Nothing to fetch"))
            self._ui(lambda: self.progress_label.pack_forget())
            self._ui(lambda: self.progress_bar.pack_forget())
            return

        total = len(needs_art)
        fetch_results = []

        for i, game in enumerate(needs_art):
            name   = game["name"]
            app_id = game["app_id"]
            short  = name[:32]
            game_start = i / total * 100
            game_end   = (i + 1) / total * 100

            self.log(f"\n🎮 Processing: {name}")
            self._ui(lambda name=name: self.status_bar.config(text=f"Searching: {name}"))
            self._ui(lambda game_start=game_start: self.progress_var.set(game_start))
            self._ui(lambda i=i, total=total, short=short:
                     self.progress_label.config(text=f"[{i+1}/{total}] {short} — Searching SteamGridDB..."))

            sgdb_id = search_game(name, app_id)
            if sgdb_id is False:
                self.log("  ❌ Network error — will retry on next fetch")
                continue
            if sgdb_id:
                def make_cb(_short, _start, _end, _i, _tot):
                    def cb(label, step, total_steps):
                        pct = _start + (step / total_steps) * (_end - _start)
                        self._ui(lambda pct=pct: self.progress_var.set(pct))
                        self._ui(lambda label=label: self.progress_label.config(
                            text=f"[{_i+1}/{_tot}] {_short} — {label}"
                        ))
                    return cb

                results = download_all_artwork(
                    sgdb_id, app_id, prefs,
                    progress_cb=make_cb(short, game_start, game_end, i, total)
                )
                self.log(f"  ✅ Artwork saved for {name}")
                fetch_results.append({"name": name, "app_id": app_id, "results": results})
                for art_data in results.values():
                    # applied_index is None for animated slots nothing was auto-applied to.
                    if art_data and art_data.get("applied_index") is not None and art_data.get("applied_path"):
                        self.last_fetch_files.append(art_data["applied_path"])
            else:
                self.log("  ⚠️ Not found on SteamGridDB — skipping permanently")
                add_to_skip_list(app_id)

        self._ui(lambda: self.progress_var.set(100))
        self._ui(lambda n=len(fetch_results), total=total:
                 self.progress_label.config(text=f"Done! Fetched artwork for {n} of {total} game(s)."))

        if fetch_results:
            self.log("\n✅ Done! Opening results screen...")
            self._ui(lambda: self.undo_button.config(state="normal"))
        else:
            self.log("\n✅ Done! No new artwork was fetched.")

        self._ui(lambda: self.status_bar.config(text="Done!"))
        self._ui(lambda: self.fetch_button.config(state="normal", text="Fetch Missing Artwork"))
        self._ui(lambda: self.load_games())
        self._ui(lambda: self.check_steam_running())

        if fetch_results:
            self.window.after(0, lambda: self.show_results(fetch_results))

    def show_results(self, fetch_results):
        """Open the results window to review and swap applied artwork."""
        t = self.theme
        results_window = tk.Toplevel(self.window)
        results_window.title("Results — Review Artwork")
        results_window.config(bg=t["bg"])
        results_window.update_idletasks()
        win_w, win_h = 900, 750
        x = (results_window.winfo_screenwidth()  // 2) - (win_w // 2)
        y = (results_window.winfo_screenheight() // 2) - (win_h // 2)
        results_window.geometry(f"{win_w}x{win_h}+{x}+{y}")
        results_window.minsize(560, 480)
        results_window.resizable(True, True)
        # Tie the results window to the main window (and later the popup to the results
        # window) so the three form one transient chain — main behind, results in the
        # middle, popup in front — that the window manager raises together when focus
        # returns to the app.
        results_window.transient(self.window)
        # Remembered so the animated-conversion popup can sit directly on top of it.
        self.results_window = results_window

        tk.Label(results_window, text="Review Applied Artwork",
                  font=("Arial", 16, "bold"), bg=t["bg"], fg=t["fg"]).pack(pady=10)
        tk.Label(results_window, text="Click an image to view it fully sized.",
                  font=("Arial", 10), bg=t["bg"], fg=t["fg"]).pack()
        tk.Label(results_window, text="Cycle through alternatives and apply your preferred art.",
                  font=("Arial", 10), bg=t["bg"], fg=t["fg"]).pack()

        # Steam status reminder (with inline Restart Steam when Steam is running)
        steam_status_frame = tk.Frame(results_window, bg=t["bg"])
        steam_status_frame.pack(pady=2)
        if is_steam_running():
            tk.Label(steam_status_frame,
                      text="⚠️ Steam is running — restart Steam to see your new artwork",
                      font=("Arial", 9), fg=t["warn"], bg=t["bg"]).pack(side="left")
            rs_btn = self._btn(steam_status_frame, "Restart Steam", lambda: None, font=("Arial", 8))
            rs_btn.config(command=lambda: [restart_steam(),
                                           rs_btn.config(text="Restarting...", state="disabled")])
            rs_btn.pack(side="left", padx=6)
        else:
            tk.Label(steam_status_frame,
                      text="⚫ Steam is not running — launch Steam to see your new artwork",
                      font=("Arial", 9), fg=t["muted2"], bg=t["bg"]).pack(side="left")

        # Buttons are pinned to the bottom FIRST so capping the window height can
        # never push them off-screen.
        btn_frame = tk.Frame(results_window, bg=t["bg"])
        btn_frame.pack(side="bottom", pady=15)
        self._btn(btn_frame, "← Back",
                  lambda: [results_window.destroy(), self.window.lift(), self.window.focus_force()],
                  font=("Arial", 12), width=12).pack(side="left", padx=10)
        self._btn(btn_frame, "All Done!",
                  lambda: [results_window.destroy(), self.window.destroy()],
                  font=("Arial", 13, "bold"), width=12).pack(side="left", padx=10)

        canvas, scrollable_frame = self._make_scrollable_frame(results_window, padx=10, pady=5)

        for game_result in fetch_results:
            self.build_game_result_section(scrollable_frame, game_result)

        self._bind_wheel_to_canvas(canvas, canvas)

        # Size the window so the first game section is fully visible. Pin the canvas
        # to the first section's height, let tkinter compute the natural window
        # height, then cap to the screen. Since the buttons are packed at the bottom,
        # capping shrinks the canvas (not the buttons) and extra sections scroll.
        results_window.update_idletasks()
        sections = scrollable_frame.winfo_children()
        if sections:
            first_section_h = sections[0].winfo_reqheight() + 24
            canvas.config(height=first_section_h)
            results_window.update_idletasks()
            needed_h = results_window.winfo_reqheight()
            screen_h = results_window.winfo_screenheight()
            screen_w = results_window.winfo_screenwidth()
            final_h = min(needed_h, screen_h - 100)
            x = (screen_w // 2) - (win_w // 2)
            y = max((screen_h // 2) - (final_h // 2), 20)
            results_window.geometry(f"{win_w}x{final_h}+{max(x, 0)}+{y}")

    def build_game_result_section(self, parent, game_result):
        """Build the artwork review UI for a single game in the results screen."""
        t = self.theme
        name    = game_result["name"]
        app_id  = game_result["app_id"]
        results = game_result["results"]
        frame = tk.LabelFrame(parent, text=f"🎮 {name}", font=("Arial", 12, "bold"),
                               padx=10, pady=10, bg=t["bg"], fg=t["fg"])
        frame.pack(fill="x", padx=10, pady=8)

        art_labels = {"grids": "Cover", "grids_wide": "Wide Cover",
                      "heroes": "Hero/Background", "logos": "Logo", "icons": "Icon"}
        for art_type, label in art_labels.items():
            art_data = results.get(art_type)
            if not art_data:
                continue

            row = tk.Frame(frame, bg=t["bg"])
            row.pack(fill="x", pady=6, anchor="center")
            tk.Label(row, text=label, font=("Arial", 10, "bold"), width=16, anchor="w",
                      bg=t["bg"], fg=t["fg"]).pack(side="left")

            state = {
                "index": 0,
                "paths": list(art_data["thumb_paths"]),
                "option_urls": art_data["option_urls"],
                "option_meta": art_data.get("option_meta", [{}] * len(art_data["option_urls"])),
                "applied_path": art_data["applied_path"],
                # None when nothing was auto-applied (animated-only slot).
                "applied_index": art_data.get("applied_index"),
                "art_type": art_type,
                "app_id": app_id,
            }

            # Fixed-size container so the badge can be positioned absolutely on top
            img_frame = tk.Frame(row, width=300, height=200, bg=t["placeholder"])
            img_frame.pack_propagate(False)
            img_frame.pack(padx=5)
            img_label = tk.Label(img_frame, bg=t["placeholder"], fg=t["fg"],
                                  text="loading...", wraplength=200)
            img_label.place(x=0, y=0, relwidth=1, relheight=1)
            badge_label = tk.Label(img_frame, text="", bg="#111111", fg="white",
                                    font=("Arial", 11), padx=3, pady=1)
            state["badge_label"] = badge_label

            counter_label = tk.Label(row, text=f"1 / {len(art_data['option_urls'])}",
                                      font=("Arial", 9), bg=t["bg"], fg=t["fg"])
            counter_label.pack(side="left", padx=4)

            def load_thumb(path, lbl=img_label):
                meta0 = state["option_meta"][0] if state["option_meta"] else {}
                def on_anim(is_anim, _m=meta0):
                    if badge_label and is_anim != _m.get("animated"):
                        _m["animated"] = is_anim
                        _update_badge(badge_label, _m)
                _display_image_on_label(lbl, path, on_anim)

            _update_badge(badge_label, state["option_meta"][0] if state["option_meta"] else {})
            load_thumb(art_data["thumb_paths"][0])

            # The auto-applied static option (if any) is index 0 on arrival; animated
            # slots auto-apply nothing, so their button starts as "Apply this one".
            apply_btn = self._btn(
                row, "✅ Applied!" if state["applied_index"] == 0 else "Apply this one",
                lambda: None)
            apply_btn.pack(side="right", padx=6)

            # Clicking the image or badge opens the full-size version in the browser
            for w in (img_frame, img_label, badge_label):
                w.bind("<Button-1>", lambda e, s=state: webbrowser.open(s["option_urls"][s["index"]]))
                w.config(cursor="hand2")

            def make_callbacks(s, il, cl, ab):
                def prev():
                    if s["index"] > 0:
                        s["index"] -= 1
                        update_view(s, il, cl)
                        ab.config(text="✅ Applied!" if s["index"] == s["applied_index"] else "Apply this one")
                def nxt():
                    if s["index"] < len(s["option_urls"]) - 1:
                        s["index"] += 1
                        update_view(s, il, cl)
                        ab.config(text="✅ Applied!" if s["index"] == s["applied_index"] else "Apply this one")
                def apply():
                    idx = s["index"]
                    url = s["option_urls"][idx]
                    meta = s["option_meta"][idx] if idx < len(s["option_meta"]) else {}
                    base_noext = s["applied_path"].rsplit(".", 1)[0]
                    # Animated picks need APNG conversion (Steam ignores webp/gif), which
                    # is slow — run it with a progress popup instead of blocking the UI.
                    if meta.get("animated"):
                        self.apply_animated_art(s, ab, url, base_noext + ".png", idx)
                        return
                    new_path = f"{base_noext}.{url.split('.')[-1].split('?')[0]}"
                    try:
                        r = requests.get(url, stream=True, timeout=10)
                        if r.status_code == 200:
                            # Remove the previously-applied file in this slot first, so
                            # an extension change (e.g. animated .png -> static .jpg)
                            # doesn't leave a duplicate that Steam keeps showing.
                            clear_slot_files(new_path)
                            with open(new_path, "wb") as f:
                                for chunk in r.iter_content(1024):
                                    f.write(chunk)
                            register_managed_file(new_path)
                            # Icons must also be registered in shortcuts.vdf to apply.
                            if s["art_type"] == "icons":
                                set_shortcut_icon(s["app_id"], new_path)
                            s["applied_index"] = idx
                            ab.config(text="✅ Applied!")
                    except Exception:
                        ab.config(text="Failed — retry")
                return prev, nxt, apply

            prev_fn, next_fn, apply_fn = make_callbacks(state, img_label, counter_label, apply_btn)
            apply_btn.config(command=apply_fn)
            self._btn(row, "◀", prev_fn).pack(side="left")
            self._btn(row, "▶", next_fn).pack(side="left")

    def apply_animated_art(self, state, apply_btn, url, out_path, index):
        """Apply an animated artwork pick by converting it to APNG (the only animated
        format Steam renders), shown with a modal progress popup. The conversion is
        slow, so it runs on a worker thread and reports progress back to the popup."""
        t = self.theme
        apply_btn.config(text="Converting…", state="disabled")

        # Sit the popup on top of the results window (it's launched from there). Being
        # transient to the results window keeps that window as its master, so the WM
        # raises the results window with the popup instead of pushing it behind the
        # main game-list window.
        parent = getattr(self, "results_window", None)
        if parent is None or not parent.winfo_exists():
            parent = self.window

        popup = tk.Toplevel(parent)
        popup.title("Applying animated artwork")
        popup.config(bg=t["bg"])
        popup.resizable(False, False)
        # transient(parent) keeps the popup above the results window within the app's
        # own window stack (background: main window, mid: results, front: popup) without
        # forcing it above other applications when focus moves elsewhere.
        popup.transient(parent)
        # Block closing while the conversion is in flight.
        popup.protocol("WM_DELETE_WINDOW", lambda: None)
        px = parent.winfo_rootx() + 60
        py = parent.winfo_rooty() + 60
        popup.geometry(f"360x120+{px}+{py}")
        tk.Label(popup, text="Converting to APNG so Steam can animate it…",
                 font=("Arial", 10), bg=t["bg"], fg=t["fg"], wraplength=320).pack(pady=(16, 6))
        status = tk.Label(popup, text="Starting…", font=("Arial", 9),
                          bg=t["bg"], fg=t["muted2"])
        status.pack()
        # Real per-frame progress comes from find_games.download_apng (it counts the
        # APNG frame chunks Pillow writes), so this is an accurate determinate bar.
        bar = ttk.Progressbar(popup, mode="determinate", maximum=100, length=300)
        bar.pack(pady=10)
        try:
            popup.grab_set()
        except tk.TclError:
            pass
        popup.lift()

        last_pct = {"v": -1}

        def on_progress(done, total):
            pct = int(done / total * 100) if total else 0
            if pct == last_pct["v"]:
                return
            last_pct["v"] = pct
            def paint():
                if bar.winfo_exists():
                    bar["value"] = pct
                    status.config(text=f"Converting frame {done} / {total}")
            self.window.after(0, paint)

        def on_status(msg):
            self.window.after(0, lambda m=msg: status.config(text=m)
                              if status.winfo_exists() else None)

        def finish(ok):
            if popup.winfo_exists():
                popup.grab_release()
                popup.destroy()
            if ok:
                state["applied_index"] = index
                apply_btn.config(text="✅ Applied!", state="normal")
            else:
                apply_btn.config(text="Failed — retry", state="normal")

        def worker():
            try:
                # Drop any existing file in this slot first so the new APNG is the only
                # one Steam sees, then write the converted .png.
                clear_slot_files(out_path)
                ok = download_apng(url, out_path, progress_cb=on_progress, status_cb=on_status)
            except Exception:
                ok = False
            self.window.after(0, lambda: finish(ok))

        threading.Thread(target=worker, daemon=True).start()


def _run_animation(lbl, img, frame):
    """Advance one frame of an animated image on a label. Silently stops if the widget is destroyed."""
    try:
        img.seek(frame % img.n_frames)
        copy = img.copy()
        copy.thumbnail((300, 200))
        photo = ImageTk.PhotoImage(copy)
        lbl.config(image=photo, text="", width=photo.width(), height=photo.height())
        lbl.image = photo
        delay = img.info.get("duration", 100)
        lbl._anim_id = lbl.after(delay, lambda: _run_animation(lbl, img, frame + 1))
    except Exception:
        pass


def _update_badge(badge_label, meta):
    """Show or hide the top-left badge overlay based on image metadata."""
    parts = []
    if meta.get("animated"): parts.append("▶")
    if meta.get("nsfw"):     parts.append("🍆")
    if meta.get("humor"):    parts.append("🤡")
    text = " ".join(parts)
    badge_label.config(text=text)
    if text:
        badge_label.place(x=4, y=4)
    else:
        badge_label.place_forget()


def _display_image_on_label(lbl, path, on_animated=None):
    """Display the image at path on lbl. Plays frame-by-frame if the image is animated.
    on_animated(bool) is called with the actual animation status detected from the file."""
    if hasattr(lbl, "_anim_id") and lbl._anim_id:
        try:
            lbl.after_cancel(lbl._anim_id)
        except Exception:
            pass
        lbl._anim_id = None
    try:
        img = Image.open(path)
        is_animated = getattr(img, "n_frames", 1) > 1
        if on_animated:
            on_animated(is_animated)
        if is_animated:
            _run_animation(lbl, img, 0)
        else:
            img.thumbnail((300, 200))
            photo = ImageTk.PhotoImage(img)
            lbl.config(image=photo, text="", width=photo.width(), height=photo.height())
            lbl.image = photo
    except Exception:
        lbl.config(text="preview\nunavailable")


# update_view is a standalone helper rather than a class method because it
# operates only on the state dict and widget references passed to it,
# with no need to access the broader application state.
def update_view(state, img_label, counter_label):
    """Load and display the thumbnail at the current index in the results screen."""
    index = state["index"]
    counter_label.config(text=f"{index + 1} / {len(state['option_urls'])}")

    badge_label = state.get("badge_label")
    meta = state["option_meta"][index] if index < len(state.get("option_meta", [])) else {}
    if badge_label:
        _update_badge(badge_label, meta)

    # Correct the badge if the actual file's animation status differs from SGDB metadata
    def on_animated(is_anim, _meta=meta, _bl=badge_label):
        if _bl and is_anim != _meta.get("animated"):
            _meta["animated"] = is_anim
            _update_badge(_bl, _meta)

    if index < len(state["paths"]):
        _display_image_on_label(img_label, state["paths"][index], on_animated)
    else:
        # Fetch on demand — keep the current image visible while downloading to
        # avoid the label reverting to character-unit dimensions mid-load.
        counter_label.config(text=f"{index + 1} / {len(state['option_urls'])} (loading...)")
        url = state["option_urls"][index]

        def fetch(expected=index, _on_anim=on_animated):
            try:
                r = requests.get(url, stream=True, timeout=10)
                ext = url.split(".")[-1].split("?")[0]
                cache_path = state["paths"][0].rsplit("_", 1)[0] + f"_{expected}.{ext}"
                with open(cache_path, "wb") as f:
                    f.write(r.content)
                state["paths"].append(cache_path)
                if state["index"] == expected:
                    img_label.after(0, lambda: _display_image_on_label(img_label, cache_path, _on_anim))
                    counter_label.after(0, lambda: counter_label.config(
                        text=f"{expected + 1} / {len(state['option_urls'])}"))
            except Exception:
                if state["index"] == expected:
                    img_label.after(0, lambda: img_label.config(text="preview\nunavailable"))

        threading.Thread(target=fetch, daemon=True).start()


if __name__ == "__main__":
    window = tk.Tk()
    app = SteamArtApp(window)
    window.mainloop()
