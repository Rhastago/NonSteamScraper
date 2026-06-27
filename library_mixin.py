"""Main-window construction + the game-library list for NonSteamScraper:
build_ui, the onboarding banner, the activity-log drawer toggle, loading games,
and rendering / sorting / searching / row-building (including cover thumbnails and
the Hidden section) plus per-game skip/reset/refetch. Mixed into SteamArtApp."""

import os
import glob
import tkinter as tk
from tkinter import ttk

from theming import PAD_XS, PAD_S, PAD_M, PAD_L, FONT_UI, FONT_MONO
from appcommon import FIRST_RUN_FILE
from imaging import _decode_row_thumb
import find_games as fg
from find_games import get_non_steam_games, load_api_key, SKIP_FILE

class LibraryMixin:

    def check_first_run(self):
        """Show the quick-start guide automatically on the first launch."""
        if not os.path.exists(FIRST_RUN_FILE):
            with open(FIRST_RUN_FILE, "w") as f:
                f.write("done")
            self.window.after(300, self.show_info)


    def _build_onboarding_banner(self):
        """Create the onboarding banner (once). It remains hidden until
        _refresh_onboarding() decides to show it based on API-key state."""
        t = self.theme
        banner = tk.Frame(self.window, bg=t["entry_bg"], pady=PAD_S)
        inner = tk.Frame(banner, bg=t["entry_bg"])
        inner.pack(expand=True)
        tk.Label(
            inner,
            text="Add your free SteamGridDB API key to start fetching artwork",
            font=(FONT_UI, 11),
            bg=t["entry_bg"],
            fg=t["fg"],
        ).pack(side="left", padx=(0, PAD_M))
        self._btn(inner, "Open Settings", self.open_settings, primary=True,
                  font=(FONT_UI, 10, "bold"), padx=PAD_S, pady=PAD_XS).pack(side="left")
        self._onboarding_banner = banner
        self._onboarding_banner_visible = False


    def _refresh_onboarding(self):
        """Show or hide the onboarding banner depending on whether an API key
        exists. Safe to call at any time — idempotent."""
        if not hasattr(self, "_onboarding_banner"):
            return
        has_key = bool(load_api_key())
        if has_key and self._onboarding_banner_visible:
            self._onboarding_banner.pack_forget()
            self._onboarding_banner_visible = False
        elif not has_key and not self._onboarding_banner_visible:
            # Pack between the header and the summary label
            self._onboarding_banner.pack(
                fill="x", before=self.summary_label
            )
            self._onboarding_banner_visible = True


    def build_ui(self):
        """Construct all main window UI elements."""
        t = self.theme

        header_frame = self._frame(self.window)
        header_frame.pack(fill="x", padx=PAD_L, pady=PAD_M)
        # Title + a thin accent underline beneath it (tasteful brand touch).
        title_box = self._frame(header_frame)
        title_box.pack(side="left")
        self._label(title_box, "NonSteamScraper", font=(FONT_UI, 20, "bold")).pack(anchor="w")
        tk.Frame(title_box, bg=t["accent"], height=3).pack(fill="x", pady=(PAD_XS, 0))
        reload_btn = self._flat_btn(header_frame, "🔄", self.refresh_library, font=(FONT_UI, 12))
        self._iconify(reload_btn, "refresh", self.ICON_TOOLBAR)
        reload_btn.pack(side="left", padx=PAD_S)
        self._add_tooltip(reload_btn, "Reload")
        art_btn = self._flat_btn(header_frame, "🎨", self.open_art_prefs, font=(FONT_UI, 12))
        self._iconify(art_btn, "palette", self.ICON_TOOLBAR)
        art_btn.pack(side="left", padx=PAD_XS)
        self._add_tooltip(art_btn, "Art Style Preferences")
        settings_btn = self._flat_btn(header_frame, "⚙", self.open_settings, font=(FONT_UI, 14))
        self._iconify(settings_btn, "settings", self.ICON_TOOLBAR)
        settings_btn.pack(side="right")
        self._add_tooltip(settings_btn, "Settings")
        info_btn = self._flat_btn(header_frame, "ℹ", self.show_info, font=(FONT_UI, 14))
        self._iconify(info_btn, "info", self.ICON_TOOLBAR)
        info_btn.pack(side="right", padx=PAD_S)
        self._add_tooltip(info_btn, "Information")

        self.summary_label = self._label(self.window, "Loading...", font=(FONT_UI, 11))
        self.summary_label.pack(pady=(PAD_S, 0))

        # Onboarding banner — built here (after summary_label exists so it can
        # use before=) then shown/hidden by _refresh_onboarding() as needed.
        self._build_onboarding_banner()
        self._refresh_onboarding()

        # Search/filter bar — live-filters the visible game list by name.
        search_frame = self._frame(self.window)
        search_frame.pack(fill="x", padx=PAD_L, pady=(PAD_S, 0))
        search_icon = self._label(search_frame, "🔍", font=(FONT_UI, 12))
        ic = self._icon("search", self.ICON_INLINE)
        if ic:
            search_icon.config(image=ic, text="", compound="left")
        search_icon.pack(side="left", padx=(0, PAD_S))
        self.search_var = tk.StringVar()
        # Debounce so a large library isn't rebuilt on every keystroke; the list
        # only re-renders once typing pauses (matches the SGDB search dialog).
        self._search_after_id = None
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
        self.search_entry = tk.Entry(search_frame, textvariable=self.search_var,
                                     font=(FONT_UI, 11), bg=t["entry_bg"], fg=t["fg"],
                                     insertbackground=t["fg"], relief="flat", bd=4,
                                     highlightthickness=1, highlightbackground=t["entry_bg"],
                                     highlightcolor=t["accent"])
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Escape>", lambda e: self._clear_search())
        clear_btn = self._flat_btn(search_frame, "✕", self._clear_search, font=(FONT_UI, 11))
        clear_btn.pack(side="left", padx=(PAD_S, 0))
        self._add_tooltip(clear_btn, "Clear search")

        # Sort row — chooses how the visible list is ordered. Sits on top of the
        # search filter (sort applies to whatever the search leaves visible).
        sort_frame = self._frame(self.window)
        sort_frame.pack(fill="x", padx=PAD_L, pady=(PAD_XS, 0))
        self._label(sort_frame, "Sort:", font=(FONT_UI, 10)).pack(
            side="left", padx=(0, PAD_S))

        # User-facing labels mapped to internal sort keys.
        self._sort_labels = {
            "Name": "name",
            "Date added": "added",
            "Missing artwork": "missing",
            "Recently fetched": "fetched",
        }
        # Reverse map so we can show the current key's label as the menu value.
        key_to_label = {v: k for k, v in self._sort_labels.items()}
        self._sort_var = tk.StringVar(value=key_to_label.get(self._sort_key, "Name"))

        def on_sort_changed(label):
            self._sort_key = self._sort_labels.get(label, "name")
            self._render_list()

        sort_menu = tk.OptionMenu(sort_frame, self._sort_var,
                                  *self._sort_labels.keys(),
                                  command=on_sort_changed)
        # Mirror the Settings account-picker theming so the dropdown matches the
        # app, and additionally style the popup menu itself (colors + font).
        sort_menu.config(bg=t["button_bg"], fg=t["button_fg"],
                         activebackground=t["select_bg"],
                         activeforeground=t["fg"], font=(FONT_UI, 10),
                         highlightthickness=0, relief="flat", cursor="hand2")
        sort_menu["menu"].config(bg=t["entry_bg"], fg=t["fg"],
                                 activebackground=t["accent"],
                                 activeforeground=t["accent_fg"],
                                 font=(FONT_UI, 10))
        sort_menu.pack(side="left")

        # Direction toggle: ▲ ascending / ▼ descending.
        def toggle_sort_dir():
            self._sort_desc = not self._sort_desc
            self._sort_dir_btn.config(text="▼" if self._sort_desc else "▲")
            self._render_list()

        self._sort_dir_btn = self._btn(
            sort_frame, "▼" if self._sort_desc else "▲", toggle_sort_dir,
            font=(FONT_UI, 10), padx=PAD_S, pady=0)
        self._sort_dir_btn.pack(side="left", padx=(PAD_S, 0))
        self._add_tooltip(self._sort_dir_btn,
                          lambda: "Descending" if self._sort_desc else "Ascending")

        list_container = self._frame(self.window)
        list_container.pack(fill="both", expand=True, padx=PAD_L, pady=PAD_M)

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
        self.progress_label = self._label(self.window, "", font=(FONT_UI, 9))
        # Accent-colored progress bar. ttk theming is finicky across platforms, so
        # build the named style defensively and fall back to the default bar.
        try:
            ttk.Style().configure("Accent.Horizontal.TProgressbar",
                                   background=t["accent"], troughcolor=t["entry_bg"])
            self.progress_bar = ttk.Progressbar(
                self.window, variable=self.progress_var, maximum=100,
                style="Accent.Horizontal.TProgressbar")
        except Exception:
            self.progress_bar = ttk.Progressbar(
                self.window, variable=self.progress_var, maximum=100)

        self.fetch_button = self._btn(self.window, "Fetch Missing Artwork",
                                       self.start_fetch, primary=True,
                                       font=(FONT_UI, 13, "bold"), padx=PAD_M, pady=PAD_S)
        self.fetch_button.pack(pady=PAD_M)

        # Undo button — enabled only after a successful fetch
        self.undo_button = self._btn(self.window, "Undo Last Fetch",
                                     self.undo_fetch, font=(FONT_UI, 10))
        self._iconify(self.undo_button, "undo", self.ICON_ACTION, compound="left")
        self.undo_button.config(state="disabled")
        self.undo_button.pack(pady=PAD_S)

        # Activity log — collapsed into a "Details" drawer by default so the main
        # view stays clean; the status bar below is the everyday signal and the
        # progress bar covers fetches. Power users expand this for the full log.
        self._log_visible = False
        self.log_toggle = self._flat_btn(self.window, "▶  Details", self._toggle_log,
                                          font=(FONT_UI, 9), anchor="w")
        self.log_toggle.pack(fill="x", padx=PAD_L, pady=(PAD_XS, 0))

        self.log_frame = self._frame(self.window)
        log_scrollbar = tk.Scrollbar(self.log_frame)
        log_scrollbar.pack(side="right", fill="y")
        self.log_box = tk.Text(self.log_frame, height=7, font=(FONT_MONO, 10), state="disabled",
                                yscrollcommand=log_scrollbar.set, bg=t["entry_bg"],
                                fg=t["fg"], insertbackground=t["fg"],
                                highlightthickness=0, relief="flat")
        self.log_box.pack(fill="both", expand=True)
        log_scrollbar.config(command=self.log_box.yview)
        # log_frame intentionally not packed yet — drawer starts collapsed.

        self.status_bar = self._label(self.window, "Ready", font=(FONT_UI, 9), anchor="w")
        self.status_bar.pack(fill="x", padx=PAD_L, pady=PAD_S)


    def _toggle_log(self):
        """Expand/collapse the activity-log drawer. Collapsed by default to keep the
        main window uncluttered; the log keeps recording either way."""
        if self._log_visible:
            self.log_frame.pack_forget()
            self.log_toggle.config(text="▶  Details")
            self._log_visible = False
        else:
            self.log_frame.pack(fill="both", padx=PAD_L, pady=PAD_S, before=self.status_bar)
            self.log_toggle.config(text="▼  Details")
            self._log_visible = True
            self.log_box.see("end")
        # Grow/shrink the window so the drawer is fully visible (open) or the
        # freed space is reclaimed (close), clamped to the work area.
        self._refit_window_height()


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
        self.log("Reloading library...", icon="refresh")
        self.load_games()
        self._set_status("Ready")
        self.log("Library reloaded — done.", icon="applied")
        self.check_steam_running()


    def load_games(self):
        """Reload the game list from Steam and refresh the UI."""
        self.games = get_non_steam_games()
        # Stamp the original add-order (shortcuts.vdf order) so the "Date added"
        # sort stays stable even after the search filter reorders the view.
        for i, g in enumerate(self.games):
            g["_added_idx"] = i
        needs_art = [g for g in self.games if not g["has_art"]]

        # Summary + fetch button reflect the full library, not the filtered view.
        if not self.games:
            self.summary_label.config(text="No non-Steam games found")
            self.fetch_button.config(state="disabled", text="Nothing to fetch")
        else:
            self.summary_label.config(
                text=f"Found {len(self.games)} Non-Steam Games — {len(needs_art)} need artwork")
            self.fetch_button.config(
                state="disabled" if not needs_art else "normal",
                text="Nothing to fetch" if not needs_art else "Fetch Missing Artwork"
            )

        self._render_list()


    def _render_list(self):
        """(Re)build the displayed rows from self.games, applying the current search
        filter. Does not re-fetch data or mutate self.games — it only chooses which
        rows to display, so filtering stays cheap and reversible."""
        self.selected_row = None
        self.selected_btn = None
        self.selected_rename_btn = None
        self.selected_name_lbl = None

        for widget in self.list_frame.winfo_children():
            widget.destroy()

        if not self.games:
            # No non-Steam games in the library — explain how to add some.
            self._label(
                self.list_frame,
                "No non-Steam games were found in your Steam library.\n\n"
                "Add one in Steam first:\n"
                "Games -> Add a Non-Steam Game to My Library,\n"
                "then click the refresh button above to reload.",
                font=(FONT_UI, 11), justify="left", anchor="w"
            ).pack(fill="x", padx=10, pady=20)
            self._size_list_canvas(min_height=40)
            return

        query = self.search_var.get().strip().lower()
        skipped = [g for g in self.games if g.get("skipped")]
        normal  = [g for g in self.games if not g.get("skipped")]
        if query:
            normal  = [g for g in normal  if query in g["name"].lower()]
            skipped = [g for g in skipped if query in g["name"].lower()]

        # Apply the chosen sort on top of the search filter so both sections stay
        # consistently ordered.
        normal  = self._sort_games(normal)
        skipped = self._sort_games(skipped)

        # The 2px accent box wraps ONLY the game rows (not the Hidden section or the
        # empty/no-result messages). Rows go inside this box, separated by single 2px
        # dividers; build_game_row packs into self._row_parent.
        if normal:
            t = self.theme
            rows_box = tk.Frame(self.list_frame, bg=t["bg"], highlightthickness=2,
                                highlightbackground=t["accent"], highlightcolor=t["accent"])
            rows_box.pack(fill="x")
            self._row_parent = rows_box
            for game in normal:
                self.build_game_row(game)

        if skipped:
            self.build_hidden_section(skipped)

        if query and not normal and not skipped:
            self._label(self.list_frame, "No games match your search.",
                        font=(FONT_UI, 11), anchor="w").pack(fill="x", padx=10, pady=20)

        # Re-attach wheel scrolling to the freshly built rows.
        self._size_list_canvas()


    def _sort_games(self, games_list):
        """Return a new list sorted by the current sort key/direction.

        Each key computes an ASCENDING sort value; the whole list is reversed
        when self._sort_desc is set. Never raises — any stat failure on the
        cover-art file is treated as mtime 0.0."""
        key = self._sort_key

        if key == "added":
            keyfn = lambda g: g.get("_added_idx", 0)
        elif key == "missing":
            # Games needing art come first ascending; name breaks ties so the
            # order is stable/readable.
            keyfn = lambda g: (0 if not g.get("has_art") else 1,
                               g["name"].lower())
        elif key == "fetched":
            # By cover-art file mtime. Stat once into a local cache so a search
            # keystroke (which re-renders) doesn't re-stat every file. Newest
            # first is the natural expectation, which falls out of sorting the
            # ascending (mtime, name) key and reversing for descending.
            mtimes = {}
            for g in games_list:
                app_id = g.get("app_id")
                mt = 0.0
                try:
                    path = fg.row_thumbnail_path(fg.GRID_FOLDER, app_id)
                    if path and os.path.exists(path):
                        mt = os.path.getmtime(path)
                except Exception:
                    mt = 0.0
                mtimes[app_id] = mt
            keyfn = lambda g: (mtimes.get(g.get("app_id"), 0.0),
                               g["name"].lower())
        else:  # "name" (default)
            keyfn = lambda g: g["name"].lower()

        return sorted(games_list, key=keyfn, reverse=self._sort_desc)


    def _on_search_changed(self):
        """Debounced search handler: coalesce rapid keystrokes into a single
        list re-render so large libraries stay responsive while typing."""
        if self._search_after_id is not None:
            try:
                self.window.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.window.after(250, self._render_list)


    def _clear_search(self):
        """Clear the search box and restore the full list."""
        self.search_var.set("")
        self.search_entry.focus_set()


    def _size_list_canvas(self, min_height=None):
        """Fit the list canvas to its content (capped at a third of the screen) and
        re-bind wheel scrolling to the freshly built rows."""
        self.window.update_idletasks()
        content_h = self.list_frame.winfo_reqheight()
        screen_h = self.window.winfo_screenheight()
        if min_height is not None:
            content_h = max(content_h, min_height)
        self.list_canvas.config(height=min(content_h, screen_h // 3))
        self._bind_wheel_to_canvas(self.list_canvas, self.list_canvas)


    def _make_thumb_placeholder(self, parent, bg):
        """Build the "needs art" chip: a small flat box in theme colors with a subtle
        border and the palette glyph, so an empty cover reads at a glance. `bg` is the
        row background so the chip's frame matches selection/normal state."""
        t = self.theme
        chip = tk.Frame(parent, width=self.ROW_THUMB_W, height=self.ROW_THUMB_H,
                        bg=t["entry_bg"], highlightthickness=1,
                        highlightbackground=t["muted2"], highlightcolor=t["muted2"])
        chip.pack_propagate(False)
        glyph = tk.Label(chip, bg=t["entry_bg"])
        ic = self._icon("palette", self.ICON_INLINE)
        if ic:
            glyph.config(image=ic)
        else:
            glyph.config(text="art", fg=t["placeholder"], font=(FONT_UI, 7))
        glyph.pack(expand=True)
        return chip


    def _attach_row_thumb(self, parent, game, row_bg):
        """Pack a cover thumbnail (or a "needs art" placeholder chip) at the left of a
        game row. Reuses cached PhotoImages keyed by (app_id, path, mtime, size) so the
        every-keystroke re-render doesn't re-decode; a changed mtime (e.g. after a
        re-fetch swaps art) naturally produces a new key and re-decodes. Decoding runs
        on the bounded thumbnail executor; assignment happens on the UI thread."""
        t = self.theme
        # fg.GRID_FOLDER (not the by-value import) so account switches are honored.
        path = fg.row_thumbnail_path(fg.GRID_FOLDER, game["app_id"])
        if path:
            try:
                st = os.stat(path)
                key = (game["app_id"], path, st.st_mtime, st.st_size)
            except OSError:
                path = None

        if not path:
            # No cover on disk -> placeholder chip.
            chip = self._make_thumb_placeholder(parent, row_bg)
            chip.pack(side="left", padx=(2, 4))
            return chip

        # Fixed-pixel container reserves the row height BEFORE the async image lands.
        # (An empty tk.Label sizes width/height in characters/lines, not pixels, so
        # setting width=28/height=42 on a not-yet-imaged label would flash a giant box
        # until the decode finishes. A Frame sizes in pixels, so wrap the label.)
        box = tk.Frame(parent, width=self.ROW_THUMB_W, height=self.ROW_THUMB_H, bg=row_bg)
        box.pack_propagate(False)
        lbl = tk.Label(box, bg=row_bg)
        lbl.pack(expand=True)
        cached = self._row_thumb_cache.get(key)
        if cached is not None:
            # Cache hit: assign synchronously, no decode/thread.
            lbl.config(image=cached)
            lbl.image = cached
        else:
            _decode_row_thumb(self._thumb_executor, lbl, path,
                              (self.ROW_THUMB_W, self.ROW_THUMB_H),
                              self._row_thumb_cache, key)
        box.pack(side="left", padx=(2, 4))
        return box


    def build_game_row(self, game):
        """Build a single game row with click-to-reveal action buttons."""
        t = self.theme
        # Border-collapse: a single 2px accent box wraps the whole list (set on
        # self.list_frame), and a single 2px accent divider sits between consecutive
        # rows — so every line is a uniform 2px, instead of each row carrying its own
        # full box (which doubled to ~4px where two rows met). Skip the divider before
        # the first row (it sits flush under the list's top border).
        if self._row_parent.winfo_children():
            tk.Frame(self._row_parent, height=2, bg=t["accent"]).pack(fill="x")
        row = tk.Frame(self._row_parent, pady=2, cursor="hand2", bg=t["bg"])
        row.pack(fill="x")

        # Cover thumbnail (or "needs art" placeholder chip) replaces the old inline
        # status icon — the cover-vs-placeholder now conveys has_art at a glance.
        thumb = self._attach_row_thumb(row, game, t["bg"])

        label = tk.Label(row, text=f"  {game['name'][:40]}",
                          font=(FONT_UI, 11), anchor="w", cursor="hand2",
                          bg=t["bg"], fg=t["fg"])
        label.pack(side="left", fill="x", expand=True)

        refetch_btn = self._btn(row, "Re-fetch", lambda g=game: self.refetch_game(g), font=(FONT_UI, 13), padx=PAD_S, pady=PAD_XS)
        rename_btn  = self._btn(row, "Search", lambda g=game: self.open_sgdb_search(g), font=(FONT_UI, 13), padx=PAD_S, pady=PAD_XS)

        def on_click(e, r=row, btn=refetch_btn, rbtn=rename_btn, lbl=label):
            # Deselect any previously selected row
            if self.selected_btn and self.selected_btn != btn:
                self.selected_btn.pack_forget()
                if self.selected_rename_btn:
                    self.selected_rename_btn.pack_forget()
                if self.selected_row:
                    self.selected_row.config(bg=t["bg"])
                    for child in self.selected_row.winfo_children():
                        child.config(bg=t["bg"])
                # Revert the previous selection's name back to normal fg.
                if self.selected_name_lbl:
                    self.selected_name_lbl.config(fg=t["fg"])

            if self.selected_btn == btn:
                # Toggle off if already selected
                btn.pack_forget()
                rbtn.pack_forget()
                r.config(bg=t["bg"])
                for child in r.winfo_children():
                    child.config(bg=t["bg"])
                lbl.config(fg=t["fg"])
                self.selected_row = self.selected_btn = self.selected_rename_btn = None
                self.selected_name_lbl = None
            else:
                # Select this row and show action buttons
                btn.pack(side="right", padx=PAD_XS)
                rbtn.pack(side="right", padx=PAD_XS)
                r.config(bg=t["select_bg"])
                for child in r.winfo_children():
                    child.config(bg=t["select_bg"])
                # Accent the selected game's name (readable on select_bg in all themes).
                lbl.config(fg=t["link"])
                self.selected_row = r
                self.selected_btn = btn
                self.selected_rename_btn = rbtn
                self.selected_name_lbl = lbl

        row.bind("<Button-1>", on_click)
        label.bind("<Button-1>", on_click)
        # Clicking the cover/placeholder selects the row too (it sits over the row's
        # click area). A cover label has no children; the placeholder chip has a glyph.
        thumb.bind("<Button-1>", on_click)
        for child in thumb.winfo_children():
            child.bind("<Button-1>", on_click)


    def build_hidden_section(self, skipped_games):
        """Build the collapsible section for games that were permanently skipped."""
        self.toggle_btn = self._flat_btn(
            self.list_frame, f"▶  Hidden ({len(skipped_games)})",
            self.toggle_hidden, font=(FONT_UI, 11), anchor="w")
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
        skip_lbl = tk.Label(row, text=f"  {game['name'][:35]}", font=(FONT_UI, 11),
                            anchor="w", fg=t["muted"], bg=t["bg"])
        self._iconify(skip_lbl, "offline", self.ICON_INLINE, compound="left")
        skip_lbl.pack(side="left", fill="x", expand=True)
        self._btn(row, "Search", lambda g=game: self.open_sgdb_search(g),
                  font=(FONT_UI, 13), padx=PAD_S, pady=PAD_XS).pack(side="right", padx=PAD_XS)
        self._btn(row, "Reset Skip", lambda g=game: self.reset_skip(g),
                  font=(FONT_UI, 13), padx=PAD_S, pady=PAD_XS).pack(side="right", padx=PAD_XS)


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
        self.log(f"Reset skip for: {game['name']}", icon="undo")
        self.load_games()


    def refetch_game(self, game):
        """Clear existing artwork and skip data so a game is fully reprocessed."""
        self._remove_from_skip(game["app_id"])
        # fg.GRID_FOLDER (not the by-value import) so account switches are honored.
        for f in glob.glob(os.path.join(fg.GRID_FOLDER, f"{game['app_id']}*")):
            os.remove(f)
        self.log(f"Reset artwork for: {game['name']} — will re-fetch on next run", icon="refresh")
        self.load_games()
