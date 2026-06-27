import tkinter as tk
from tkinter import messagebox
import sys
from concurrent.futures import ThreadPoolExecutor
import find_games as fg  # live access to rebound globals (e.g. fg.GRID_FOLDER after account switch)
from find_games import clean_old_cache

from theming import resolve_theme, load_theme, load_accent
from appcommon import VERSION
from ui_mixin import UIMixin
from geometry_mixin import GeometryMixin
from library_mixin import LibraryMixin
from fetch_mixin import FetchMixin
import dialogs
import settings_window
import results_window


class SteamArtApp(UIMixin, GeometryMixin, LibraryMixin, FetchMixin):
    # Coherent icon sizes (px) by role, so glyphs stay big and readable everywhere.
    ICON_TOOLBAR = 33   # top-bar icon-only buttons
    ICON_NAV     = 39   # prev/next artwork cycling buttons (need a clear click target)
    ICON_BTN     = 33   # other icon-only buttons (eye)
    ICON_ACTION  = 27   # icon beside button text (apply/undo/reset)
    ICON_INLINE  = 27   # indicators inside list rows / status labels
    ICON_LOG     = 24   # inline icons in the activity log

    # Row cover-thumbnail box (px). Portrait covers are ~2:3, so a 59px-tall box is
    # ~39px wide. The placeholder "needs art" chip uses the same box for alignment.
    ROW_THUMB_H  = 59
    ROW_THUMB_W  = 39

    def __init__(self, window):
        if fg.STEAM_NOT_FOUND:
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
        self.accent_name = load_accent()
        self.theme = resolve_theme(self.theme_name, self.accent_name)
        self.window.title(f"NonSteamScraper v{VERSION}")
        self.window.minsize(560, 480)
        self.window.resizable(True, True)
        self.window.config(bg=self.theme["bg"])
        # Build the whole window hidden, then size + position it and reveal it
        # once (see the deiconify after _restore_geometry) so the user never
        # sees it appear at a default spot and jump to its final geometry.
        self.window.withdraw()
        self._set_window_icon()
        self.games = []
        # Sort state for the main list (session-only; resets to Name/ascending
        # each launch). Keys: "name", "added", "missing", "fetched".
        self._sort_key = "name"
        self._sort_desc = False
        self.hidden_visible = False
        self.hidden_frame = None
        self.selected_row = None
        self.selected_btn = None
        self.selected_rename_btn = None
        self.selected_name_lbl = None
        self.last_fetch_files = []
        self._icon_cache = {}
        # Row-thumbnail cache: (app_id, path, mtime, size) -> ImageTk.PhotoImage.
        # Keyed including mtime+size so a re-fetch that swaps art invalidates the old
        # entry. Holding the PhotoImage here keeps a live reference so tkinter doesn't
        # GC it (a dropped reference renders as a blank image). _render_list runs on
        # every search keystroke, so reusing decoded images here avoids re-decoding.
        self._row_thumb_cache = {}
        # Single bounded worker pool drains decode work so a large library building
        # hundreds of rows can't spawn hundreds of threads. UI assignment is always
        # scheduled back on the Tk thread via window.after().
        self._thumb_executor = ThreadPoolExecutor(max_workers=2)
        # Stack of currently-open modal Toplevels (bottom -> top). _open_modal pushes,
        # _close_modal pops and restores the grab to the new top (or the main window).
        self._modal_stack = []
        self.build_ui()
        clean_old_cache()
        self.load_games()
        self.check_steam_running()
        # Defer & auto-apply: flush any icons queued from a previous session if Steam
        # is closed now, then poll so newly-queued icons apply the moment Steam closes.
        self._flush_pending_icons()
        self._poll_pending_icons()
        self._autosize_window()
        # The window is centered by _autosize_window every launch; we intentionally
        # do NOT restore a saved position (see _restore_geometry).
        # Sized and positioned — reveal it now, in place, with no visible resize.
        self.window.deiconify()
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.check_first_run()
    def open_sgdb_search(self, game):
        return dialogs.open_sgdb_search(self, game)

    def show_info(self):
        return dialogs.show_info(self)

    def open_art_prefs(self):
        return dialogs.open_art_prefs(self)

    def open_settings(self):
        return settings_window.open_settings(self)

    def show_results(self, fetch_results):
        return results_window.show_results(self, fetch_results)





if __name__ == "__main__":
    window = tk.Tk()
    app = SteamArtApp(window)
    window.mainloop()
