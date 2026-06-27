import tkinter as tk
from tkinter import ttk, messagebox
import threading
import time
import requests
import os
import sys
import glob
import re
import webbrowser
from PIL import Image, ImageTk
import find_games as fg  # live access to rebound globals (e.g. fg.GRID_FOLDER after account switch)
from find_games import (
    get_non_steam_games, search_game, download_all_artwork,
    load_api_key, save_api_key, verify_api_key,
    add_to_skip_list, SKIP_FILE, save_name_override, get_cache_size,
    clear_cache, clean_old_cache,
    is_steam_running, restart_steam, get_all_steam_users,
    clear_managed_artwork, register_managed_file, set_shortcut_icon,
    load_prefs, save_prefs, DEFAULT_PREFS, STEAM_NOT_FOUND, full_reset,
    search_sgdb_autocomplete, clear_slot_files, download_apng,
    parse_net_workarea, compute_window_fit,
)

VERSION = "1.3.0"

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
    # Coherent icon sizes (px) by role, so glyphs stay big and readable everywhere.
    ICON_TOOLBAR = 33   # top-bar icon-only buttons
    ICON_NAV     = 39   # prev/next artwork cycling buttons (need a clear click target)
    ICON_BTN     = 33   # other icon-only buttons (eye)
    ICON_ACTION  = 27   # icon beside button text (apply/undo/reset)
    ICON_INLINE  = 27   # indicators inside list rows / status labels
    ICON_LOG     = 24   # inline icons in the activity log

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
        # Build the whole window hidden, then size + position it and reveal it
        # once (see the deiconify after _restore_geometry) so the user never
        # sees it appear at a default spot and jump to its final geometry.
        self.window.withdraw()
        self._set_window_icon()
        self.games = []
        self.hidden_visible = False
        self.hidden_frame = None
        self.selected_row = None
        self.selected_btn = None
        self.selected_rename_btn = None
        self.last_fetch_files = []
        self._icon_cache = {}
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
        self._restore_geometry()
        # Sized and positioned — reveal it now, in place, with no visible resize.
        self.window.deiconify()
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.check_first_run()

    @staticmethod
    def _safe(fn):
        """Call fn() swallowing any Tk error (window gone / not viewable)."""
        try:
            fn()
        except Exception:
            pass

    def _open_modal(self, win):
        """Make `win` modal over the current chain and push it onto _modal_stack.

        The new top sits transient-above whatever is currently top-of-stack (or the
        main window), so nested popups stack correctly and stay visible-with-parent.
        grab_set() is an application-local grab: it blocks this app's OTHER windows
        (lower in the stack) but not other OS apps. A grab on a withdrawn / not-yet-
        mapped window raises TclError, so callers must deiconify first; we still
        update_idletasks and retry the grab once via after() to be safe."""
        parent = self._modal_stack[-1] if self._modal_stack else self.window
        try:
            win.transient(parent)
        except Exception:
            pass
        try:
            win.update_idletasks()
        except Exception:
            pass

        def _grab():
            try:
                win.grab_set()
            except tk.TclError:
                # Not viewable yet — try once more shortly.
                try:
                    win.after(10, lambda: self._safe(win.grab_set))
                except Exception:
                    pass

        _grab()
        try:
            win.lift()
        except Exception:
            pass
        try:
            win.focus_force()
        except Exception:
            pass
        self._modal_stack.append(win)

    def _close_modal(self, win):
        """End `win`'s modality and hand control back to the previous level.

        Releases win's grab, pops it off _modal_stack, then re-grabs the new top so
        the level below regains modality (a bare grab_release would otherwise drop
        modality entirely while a lower window still has its own grab). Falls back to
        focusing the main window when the stack empties. Call this on EVERY close path
        BEFORE win.destroy() so the grab can never get stuck."""
        try:
            win.grab_release()
        except Exception:
            pass
        if win in self._modal_stack:
            self._modal_stack.remove(win)
        if self._modal_stack:
            top = self._modal_stack[-1]
            self._safe(top.grab_set)
            self._safe(top.lift)
            self._safe(top.focus_force)
        else:
            self._safe(self.window.lift)
            self._safe(self.window.focus_force)

    def _set_window_icon(self):
        """Load the bundled icon for the window and all child windows.
        Silently does nothing if the icon file is missing."""
        try:
            self._icon_image = ImageTk.PhotoImage(Image.open(resource_path("icon.png")))
            self.window.iconphoto(True, self._icon_image)
        except Exception:
            pass

    def _icon(self, name, size=18):
        """Return a cached PhotoImage for assets/icons/{name}.png scaled to `size` px,
        or None if it can't be loaded — callers fall back to their emoji/text so the UI
        never breaks if an icon is missing or unbundled. Tk can't render color emoji,
        so these bundled images are how the app shows colorful icons consistently."""
        key = (name, size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        photo = None
        try:
            img = Image.open(resource_path(os.path.join("assets", "icons", f"{name}.png")))
            img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
        except Exception:
            photo = None
        self._icon_cache[key] = photo
        return photo

    def _iconify(self, button, name, size=18, compound=None):
        """Swap a button/label's emoji text for a bundled color icon. With `compound`
        set (e.g. 'left') the icon sits beside the existing text; otherwise it replaces
        the text. No-op if the icon can't load, leaving the original glyph in place."""
        ic = self._icon(name, size)
        if not ic:
            return
        if compound:
            button.config(image=ic, compound=compound)
        else:
            button.config(image=ic, text="")

    def _set_status(self, text, fg=None, icon=None):
        """Set the status bar text with an optional leading color icon. Always sets the
        image (icon or empty) so a previous icon never lingers when the text changes."""
        ic = self._icon(icon, self.ICON_INLINE) if icon else None
        self.status_bar.config(text=text, image=ic or "",
                               compound="left" if ic else "none",
                               fg=fg or self.theme["fg"])

    def _set_apply_btn(self, btn, applied, queued=False):
        """Set an Apply/Applied button's label + color icon by state, clearing any
        stale image when reverting so the icon and text stay in sync. `queued` is the
        icon-deferred case: the art downloaded but the shortcuts.vdf write is pending a
        Steam-close, so we show a distinct "Queued" label instead of "Applied!"."""
        if queued:
            # Icon write deferred while Steam is running (applies on Steam close).
            ic = self._icon("warning", self.ICON_ACTION)
            btn.config(text="Queued (close Steam)", image=ic or "",
                       compound="left" if ic else "none")
            return
        ic = self._icon("applied", self.ICON_ACTION) if applied else None
        if applied:
            btn.config(text="Applied!", image=ic or "",
                       compound="left" if ic else "none")
        else:
            btn.config(text="Apply this one", image="", compound="none")

    def _autosize_window(self):
        """Size the window to fit all its content so nothing is clipped on open,
        positioned fully within the screen work area (never under a taskbar/panel)
        and centered. Temporarily packs the progress widgets to include them in
        the measurement, then hides them again so they only appear during a fetch."""
        self.progress_label.pack(pady=2)
        self.progress_bar.pack(fill="x", padx=20, pady=2)
        self.window.update_idletasks()
        req_w = max(self.window.winfo_reqwidth(), 700)
        req_h = self.window.winfo_reqheight()
        self.progress_label.pack_forget()
        self.progress_bar.pack_forget()
        # Reuse the same work-area-aware fit as the results window so the main
        # window can't open under a taskbar/panel either. Remember the applied
        # size so _restore_geometry can clamp a saved position against it.
        self._main_w, self._main_h = self._fit_window_to_workarea(
            self.window, req_w, req_h)

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

    def _get_workarea(self, window):
        """Return (x, y, w, h) of the usable screen work area (screen minus
        taskbar/panels), so windows can be placed without landing under the
        Windows taskbar or a Linux/X11 panel.

        Detection is FLASH-FREE — it never creates a probe window (an earlier
        transparent-probe approach both flashed and returned garbage sizes on
        KDE/Plasma). Instead it asks the OS directly:
          * Windows: SystemParametersInfoW / SPI_GETWORKAREA (ctypes).
          * Linux/X11: the _NET_WORKAREA root property via `xprop`.
        Each result must pass a sanity floor (a real work area is most of the
        screen) so junk values can't shrink the window to a tiny square. Falls
        back to the full screen minus a bottom taskbar margin if both fail.
        """
        window.update_idletasks()
        screen_w = window.winfo_screenwidth()
        screen_h = window.winfo_screenheight()

        def _sane(x, y, w, h):
            # A genuine work area covers most of the screen; reject anything
            # tiny (garbage) or larger than the screen, and any negative origin.
            return (x >= 0 and y >= 0
                    and w >= screen_w * 0.5 and h >= screen_h * 0.5
                    and w <= screen_w and h <= screen_h)

        # --- Windows: direct OS query, no window, no flash. ---
        try:
            import ctypes
            from ctypes import wintypes
            SPI_GETWORKAREA = 0x0030
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                wa = (rect.left, rect.top,
                      rect.right - rect.left, rect.bottom - rect.top)
                if _sane(*wa):
                    return wa
        except (AttributeError, OSError, ValueError):
            # Not on Windows / no user32 — fall through.
            pass

        # --- Linux/X11: read _NET_WORKAREA from the root window via xprop. ---
        try:
            import subprocess, shutil
            if shutil.which("xprop"):
                out = subprocess.run(
                    ["xprop", "-root", "_NET_WORKAREA"],
                    capture_output=True, text=True, timeout=1).stdout
                # Parsing delegated to the pure find_games helper so it can be
                # unit-tested without a display.
                parsed = parse_net_workarea(out)
                if parsed is not None:
                    wa = parsed
                    if _sane(*wa):
                        return wa
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

        # Fallback: full screen minus a generous bottom taskbar/panel margin.
        margin = 80
        return 0, 0, screen_w, max(screen_h - margin, 200)

    def _fit_window_to_workarea(self, window, desired_w, desired_h):
        """Size and position *window* so it sits fully inside the screen work
        area, with its title bar and all edges visible (never under a panel or
        taskbar). Centers within the work area when there is room.

        desired_w/desired_h are the preferred size; the window only shrinks below
        it when the work area is smaller.
        Returns the (final_w, final_h) actually applied.
        """
        window.update_idletasks()
        wa_x, wa_y, wa_w, wa_h = self._get_workarea(window)
        # The pure size+position math lives in find_games.compute_window_fit so it
        # can be unit-tested without a display.  The tkinter-specific side effects
        # (minsize adjustment, geometry call) stay here.
        RESERVE = 80
        final_w, final_h, x, y = compute_window_fit(
            wa_x, wa_y, wa_w, wa_h, desired_w, desired_h, reserve=RESERVE)
        # Don't let an earlier minsize() force the window bigger than the work
        # area; lower the effective minimum if the screen is small.
        avail_h = max(wa_h - RESERVE, 200)
        try:
            min_w, min_h = window.minsize()
            if min_w > wa_w or min_h > avail_h:
                window.minsize(min(min_w, wa_w), min(min_h, avail_h))
        except tk.TclError:
            pass
        window.geometry(f"{final_w}x{final_h}+{x}+{y}")
        return final_w, final_h

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
        """Restore the saved window position, clamped into the current work area.
        A position saved on a larger or different screen (e.g. the Deck docked to
        an external display, then undocked) could otherwise place the window
        off-screen or under the taskbar with no way to recover it."""
        try:
            if not os.path.exists(GEOMETRY_FILE):
                return
            with open(GEOMETRY_FILE, "r", encoding="utf-8") as f:
                geo = f.read().strip()
            # geo looks like "WxH+X+Y" (X/Y may be negative); we keep the
            # autosized size and only restore a clamped position.
            coords = re.findall(r"[+-]\d+", geo)
            if len(coords) < 2:
                return
            x, y = int(coords[0]), int(coords[1])
            wa_x, wa_y, wa_w, wa_h = self._get_workarea(self.window)
            w = getattr(self, "_main_w", 700)
            h = getattr(self, "_main_h", 480)
            x = max(wa_x, min(x, wa_x + wa_w - w))
            y = max(wa_y, min(y, wa_y + wa_h - h))
            self.window.geometry(f"+{x}+{y}")
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
            # A PyInstaller onefile bundle unpacks to a temp dir tracked via _MEI*/_PYI*
            # env vars. If the relaunched process inherits them it reuses the OLD temp
            # dir, which the exiting process then deletes — breaking compiled imports
            # like PIL._imaging on restart. Strip them so the new process re-extracts.
            env = {k: v for k, v in os.environ.items()
                   if not (k.startswith("_MEI") or k.startswith("_PYI"))}
            os.execve(sys.executable, [sys.executable], env)
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
        self._iconify(reload_btn, "refresh", self.ICON_TOOLBAR)
        reload_btn.pack(side="left", padx=6)
        self._add_tooltip(reload_btn, "Reload")
        art_btn = self._flat_btn(header_frame, "🎨", self.open_art_prefs, font=("Arial", 12))
        self._iconify(art_btn, "palette", self.ICON_TOOLBAR)
        art_btn.pack(side="left", padx=2)
        self._add_tooltip(art_btn, "Art Style Preferences")
        settings_btn = self._flat_btn(header_frame, "⚙", self.open_settings, font=("Arial", 14))
        self._iconify(settings_btn, "settings", self.ICON_TOOLBAR)
        settings_btn.pack(side="right")
        self._add_tooltip(settings_btn, "Settings")
        info_btn = self._flat_btn(header_frame, "ℹ", self.show_info, font=("Arial", 14))
        self._iconify(info_btn, "info", self.ICON_TOOLBAR)
        info_btn.pack(side="right", padx=4)
        self._add_tooltip(info_btn, "Information")

        self.summary_label = self._label(self.window, "Loading...", font=("Arial", 11))
        self.summary_label.pack()

        # Search/filter bar — live-filters the visible game list by name.
        search_frame = self._frame(self.window)
        search_frame.pack(fill="x", padx=20, pady=(4, 0))
        search_icon = self._label(search_frame, "🔍", font=("Arial", 12))
        ic = self._icon("search", self.ICON_INLINE)
        if ic:
            search_icon.config(image=ic, text="", compound="left")
        search_icon.pack(side="left", padx=(0, 6))
        self.search_var = tk.StringVar()
        # Debounce so a large library isn't rebuilt on every keystroke; the list
        # only re-renders once typing pauses (matches the SGDB search dialog).
        self._search_after_id = None
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
        self.search_entry = tk.Entry(search_frame, textvariable=self.search_var,
                                     font=("Courier", 11), bg=t["entry_bg"], fg=t["fg"],
                                     insertbackground=t["fg"], relief="flat", bd=4)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Escape>", lambda e: self._clear_search())
        clear_btn = self._flat_btn(search_frame, "✕", self._clear_search, font=("Arial", 11))
        clear_btn.pack(side="left", padx=(6, 0))
        self._add_tooltip(clear_btn, "Clear search")

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
        self.undo_button = self._btn(self.window, "Undo Last Fetch",
                                     self.undo_fetch, font=("Arial", 10))
        self._iconify(self.undo_button, "undo", self.ICON_ACTION, compound="left")
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
        # Opportunistic flush: a manual refresh after closing Steam applies queued icons
        # immediately (the periodic poll is the primary mechanism).
        self._flush_pending_icons()
        if is_steam_running():
            self._set_status(
                "Steam is running — restart Steam after fetching to see changes",
                self.theme["warn"], icon="warning")

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
        self.log("Reloading library...", icon="refresh")
        self.load_games()
        self._set_status("Ready")
        self.check_steam_running()

    def load_games(self):
        """Reload the game list from Steam and refresh the UI."""
        self.games = get_non_steam_games()
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
                font=("Arial", 11), justify="left", anchor="w"
            ).pack(fill="x", padx=10, pady=20)
            self._size_list_canvas(min_height=40)
            return

        query = self.search_var.get().strip().lower()
        skipped = [g for g in self.games if g.get("skipped")]
        normal  = [g for g in self.games if not g.get("skipped")]
        if query:
            normal  = [g for g in normal  if query in g["name"].lower()]
            skipped = [g for g in skipped if query in g["name"].lower()]

        for game in normal:
            self.build_game_row(game)

        if skipped:
            self.build_hidden_section(skipped)

        if query and not normal and not skipped:
            self._label(self.list_frame, "No games match your search.",
                        font=("Arial", 11), anchor="w").pack(fill="x", padx=10, pady=20)

        # Re-attach wheel scrolling to the freshly built rows.
        self._size_list_canvas()

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

    def build_game_row(self, game):
        """Build a single game row with click-to-reveal action buttons."""
        t = self.theme
        row = tk.Frame(self.list_frame, pady=2, cursor="hand2", bg=t["bg"])
        row.pack(fill="x", padx=5)

        label = tk.Label(row, text=f"  {game['name'][:40]}",
                          font=("Courier", 11), anchor="w", cursor="hand2",
                          bg=t["bg"], fg=t["fg"])
        self._iconify(label, "applied" if game["has_art"] else "palette", self.ICON_INLINE, compound="left")
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
        skip_lbl = tk.Label(row, text=f"  {game['name'][:35]}", font=("Courier", 11),
                            anchor="w", fg=t["muted"], bg=t["bg"])
        self._iconify(skip_lbl, "offline", self.ICON_INLINE, compound="left")
        skip_lbl.pack(side="left", fill="x", expand=True)
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

        def _close_dlg(evt=None):
            self._close_modal(dlg)
            dlg.destroy()

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
            # fg.GRID_FOLDER (not the by-value import) so account switches are honored.
            for f in glob.glob(os.path.join(fg.GRID_FOLDER, f"{game['app_id']}*")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            self.log(f"Matched '{game['name']}' → '{chosen['name']}' (SGDB #{chosen['id']})",
                     icon="search")
            _close_dlg()
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
        listbox.bind("<Escape>", _close_dlg)
        entry.bind("<Return>", lambda e: (listbox.selection_set(0), _apply()) if results else None)
        entry.bind("<Down>", _entry_down)
        entry.bind("<Escape>", _close_dlg)
        listbox.bind("<Up>", _listbox_up)
        dlg.bind("<Escape>", _close_dlg)
        dlg.protocol("WM_DELETE_WINDOW", _close_dlg)

        self._btn(btn_bar, "Select", _apply).pack(side="right")
        self._btn(btn_bar, "Cancel", _close_dlg).pack(side="right", padx=(0, 6))

        # Kick off initial search with the game's current name
        _do_search(game["name"])
        self._open_modal(dlg)

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

    def undo_fetch(self):
        """Delete the files downloaded during the last fetch, reverting those games
        back to needing artwork. Works even on a first-time fetch with no prior state."""
        if not self.last_fetch_files:
            self.log("Nothing to undo.", icon="warning")
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
        self.log(f"Undo complete — removed {removed} file(s). Restart Steam to see changes.",
                 icon="undo")
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
            self._close_modal(info)
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
        self._open_modal(info)

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
            self.log("Art style preferences saved.", icon="palette")
            self._close_modal(win)
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
        reset_btn = self._btn(header_row, "Reset to defaults", do_reset, font=("Arial", 9))
        self._iconify(reset_btn, "undo", self.ICON_ACTION, compound="left")
        reset_btn.pack(side="left")
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
        self._open_modal(win)

    def open_settings(self):
        """Open the settings panel."""
        t = self.theme
        settings = tk.Toplevel(self.window)
        settings.title("Settings")
        # Build hidden, then size/position and reveal once (deiconify at the end
        # of this method) so there's no appear-then-resize flash.
        settings.withdraw()
        settings.geometry("480x520")
        settings.minsize(420, 360)
        settings.resizable(True, True)
        settings.config(bg=t["bg"])
        settings.update_idletasks()
        # Final size/position is set once at the end via _fit_window_to_workarea.

        tk.Label(settings, text="Settings", font=("Arial", 16, "bold"),
                  bg=t["bg"], fg=t["fg"]).pack(pady=10)

        def close_settings():
            self._close_modal(settings)
            settings.destroy()

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
        api_entry.pack(side="left", fill="y")
        # Select the whole key on focus (e.g. a single click) so it can be
        # replaced/copied without needing to double-click and drag. after_idle
        # runs the selection after the click's default cursor placement, so it
        # sticks.
        api_entry.bind("<FocusIn>", lambda e: e.widget.after(
            0, lambda: (e.widget.select_range(0, "end"), e.widget.icursor("end"))))
        # The eye button holds a large image (pixel-sized) while Save is text
        # (char-sized). Put both in a 2-column grid holder with equal-weight
        # `uniform` columns so the columns are forced to the same width and the
        # shared row to the same height; each button fills its cell (sticky
        # nsew) so the two render as identical boxes. Grid never clips its
        # children, so the buttons can't vanish.
        btn_holder = tk.Frame(key_row, bg=t["bg"])
        btn_holder.pack(side="left", padx=2)
        btn_holder.grid_columnconfigure(0, weight=1, uniform="keybtns")
        btn_holder.grid_columnconfigure(1, weight=1, uniform="keybtns")
        eye_btn = self._btn(btn_holder, "👁",
                  lambda: api_entry.config(show="" if api_entry.cget("show") == "*" else "*"),
                  font=("Arial", 10))
        self._iconify(eye_btn, "eye", self.ICON_BTN)
        eye_btn.grid(row=0, column=0, sticky="nsew", padx=(0, 2))

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
                            self.log("API key verified and saved.", icon="key")
                        else:
                            key_status.config(
                                text="✗ Key invalid or unreachable — not saved", fg=t["danger"])
                    except Exception:
                        pass
                self.window.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        save_btn = self._btn(btn_holder, "Save", do_save_key, font=("Arial", 9))
        save_btn.grid(row=0, column=1, sticky="nsew")

        # Appearance
        appearance_frame = tk.LabelFrame(body, text="Appearance", padx=10, pady=8,
                                          bg=t["bg"], fg=t["fg"])
        appearance_frame.pack(fill="x", padx=15, pady=5)
        tk.Label(appearance_frame, text=f"Current: {self.theme_name.capitalize()} mode",
                  font=("Arial", 10), bg=t["bg"], fg=t["fg"]).pack(side="left")
        toggle_text = "Switch to Dark Mode" if self.theme_name == "light" else "Switch to Light Mode"
        self._btn(appearance_frame, toggle_text, self.toggle_theme,
                  font=("Arial", 9)).pack(side="right")

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
            self.log("Cache cleared.", icon="trash")

        self._btn(cache_frame, "Clear Cache", do_clear_cache, font=("Arial", 9)).pack(side="right")

        # Steam
        steam_frame = tk.LabelFrame(body, text="Steam", padx=10, pady=8,
                                     bg=t["bg"], fg=t["fg"])
        steam_frame.pack(fill="x", padx=15, pady=5)
        running = is_steam_running()
        tk.Label(steam_frame, text="Status:", font=("Arial", 10),
                  bg=t["bg"], fg=t["fg"]).pack(side="left")
        steam_status_lbl = tk.Label(steam_frame,
                  text=" Running" if running else " Not running",
                  font=("Arial", 10), bg=t["bg"], fg=t["fg"])
        self._iconify(steam_status_lbl, "online" if running else "offline", self.ICON_INLINE, compound="left")
        steam_status_lbl.pack(side="left", padx=2)
        self._btn(steam_frame, "Restart Steam",
                  lambda: [restart_steam(), close_settings(),
                           self.log("Steam restarting...", icon="refresh")],
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
                self.log(f"Cleared {deleted} artwork file(s) added by NonSteamScraper.",
                         icon="trash")
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

        # Updates
        updates_frame = tk.LabelFrame(body, text="Updates", padx=10, pady=8,
                                       bg=t["bg"], fg=t["fg"])
        updates_frame.pack(fill="x", padx=15, pady=5)
        tk.Label(updates_frame, text=f"Current version: v{VERSION}",
                  font=("Arial", 10), bg=t["bg"], fg=t["fg"]).pack(anchor="w")
        update_status = tk.Label(updates_frame, text="", font=("Arial", 9),
                                  bg=t["bg"], fg=t["muted2"])
        update_status.pack(anchor="w", pady=(2, 0))
        # "Download" button shown only when an update is available.
        download_btn_holder = tk.Frame(updates_frame, bg=t["bg"])
        download_btn_holder.pack(anchor="w", pady=(2, 0))

        def do_check_update():
            check_btn.config(state="disabled")
            update_status.config(text="Checking…", fg=t["muted2"])
            # Hide any previous download button while checking.
            for w in download_btn_holder.winfo_children():
                w.destroy()

            def worker():
                result = fg.check_for_update(VERSION)

                def finish():
                    try:
                        check_btn.config(state="normal")
                        err = (result or {}).get("error") if result else None
                        if not result or err:
                            msg = (f"Could not check: {err}" if err
                                   else "Could not check for updates.")
                            update_status.config(text=msg, fg=t["warn"])
                        elif result["available"]:
                            update_status.config(
                                text=f"Update available: v{result['latest']}",
                                fg=t["ok"])
                            # Show a Download button that opens the platform link.
                            self._btn(
                                download_btn_holder,
                                f"Download v{result['latest']}",
                                lambda url=result["url"]: webbrowser.open(url),
                                font=("Arial", 9),
                            ).pack(side="left")
                        else:
                            update_status.config(
                                text=f"You're on the latest version (v{result['current']}).",
                                fg=t["muted2"])
                    except Exception:
                        pass
                self.window.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        check_btn = self._btn(updates_frame, "Check for updates", do_check_update,
                               font=("Arial", 9))
        check_btn.pack(anchor="w", pady=(4, 0))
        # Always-available manual fallback — works even when the auto-check is
        # rate-limited or offline. Same hyperlink style as the SteamGridDB link.
        releases_link = tk.Label(updates_frame, text="View releases page",
                                 font=("Arial", 9, "underline"),
                                 fg=t["link"], bg=t["bg"], cursor="hand2")
        releases_link.pack(anchor="w", pady=(4, 0))
        releases_link.bind("<Button-1>", lambda e: webbrowser.open(fg.RELEASES_URL))

        # Steam Account — always shown; interactive only when 2+ accounts found.
        # Placed after Updates so it appears at the bottom of the settings.
        users = get_all_steam_users()
        account_frame = tk.LabelFrame(body, text="Steam Account", padx=10, pady=8,
                                       bg=t["bg"], fg=t["fg"])
        account_frame.pack(fill="x", padx=15, pady=5)
        tk.Label(account_frame, text="Select account:", font=("Arial", 10),
                  bg=t["bg"], fg=t["fg"]).pack(side="left")
        if len(users) > 1:
            # Build display labels "PersonaName (id)" when known, else the bare id.
            personas = fg.get_steam_user_personas()
            label_to_id = {}
            id_to_label = {}
            for u in users:
                label = f"{personas[u]} ({u})" if u in personas else u
                label_to_id[label] = u
                id_to_label[u] = label
            active = fg.get_active_user()
            account_var = tk.StringVar(value=id_to_label.get(active, users[0]))

            def on_account_change(label):
                uid = label_to_id.get(label)
                if uid and fg.set_active_user(uid):
                    self.log(f"Switched to Steam account {label}.", icon="refresh")
                    self.load_games()  # reload shortcuts for the newly active account

            om = tk.OptionMenu(account_frame, account_var, *id_to_label.values(),
                               command=on_account_change)
            om.config(bg=t["button_bg"], fg=t["button_fg"], highlightthickness=0)
            om.pack(side="left", padx=8)
        else:
            # 0 or 1 accounts — show a disabled placeholder so the section is
            # always visible but clearly non-interactive.
            placeholder = ("Only one Steam account detected"
                           if len(users) == 1
                           else "No Steam account detected")
            om_var = tk.StringVar(value=placeholder)
            om = tk.OptionMenu(account_frame, om_var, placeholder)
            om.config(bg=t["button_bg"], fg=t["button_fg"],
                      highlightthickness=0, state="disabled")
            om.pack(side="left", padx=8)

        self._bind_wheel_to_canvas(canvas, canvas)

        # Auto-size to fit all content, fully within the screen work area (so it
        # can't open under a taskbar/panel) — same shared placement helper the
        # main and results windows use.
        settings.update_idletasks()
        body_h  = body.winfo_reqheight()
        overhead = 110  # title label + close button + padding
        body_w = body.winfo_reqwidth() + 40  # +40 for scrollbar and frame padding
        self._fit_window_to_workarea(settings, max(480, body_w), max(360, body_h + overhead))
        # Reveal now that it's correctly sized and positioned, then grab it modal.
        settings.deiconify()
        self._open_modal(settings)

    def _ui(self, fn):
        """Schedule fn to run on the main (UI) thread. Safe to call from workers."""
        self.window.after(0, fn)

    def _flush_pending_icons(self):
        """Auto-apply deferred icon writes when it's safe. No-op (cheap existence check)
        when nothing is pending or Steam is running. Steam is closed when this writes, so
        the icons show on Steam's next launch. Part of the "defer & auto-apply" design:
        no dialog, no Steam restart by us (see find_games.PENDING_ICONS_FILE)."""
        if fg.has_pending_icons() and not fg.is_steam_running():
            n = fg.apply_pending_icons()
            if n > 0:
                self.log(f"Applied {n} queued icon(s) — restart Steam to see them.",
                         icon="applied")

    def _poll_pending_icons(self):
        """Periodic timer that flushes pending icons the moment Steam is closed, then
        reschedules itself. Primary mechanism behind defer & auto-apply."""
        self._flush_pending_icons()
        self.window.after(4000, self._poll_pending_icons)

    def log(self, message, icon=None):
        """Append a message to the activity log. Thread-safe: the text-box mutation
        is marshaled onto the main thread, so this may be called from worker threads.
        An optional `icon` name embeds a bundled color icon at the start of the line."""
        def append():
            self.log_box.config(state="normal")
            ic = self._icon(icon, self.ICON_LOG) if icon else None
            msg = message
            if ic:
                # Preserve any leading blank lines, then place the icon at the line start.
                lead = len(msg) - len(msg.lstrip("\n"))
                if lead:
                    self.log_box.insert("end", "\n" * lead)
                    msg = msg[lead:]
                self.log_box.image_create("end", image=ic, padx=2)
                self.log_box.insert("end", " ")
            self.log_box.insert("end", msg + "\n")
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
        self._set_status("Working...")
        self.progress_var.set(0)
        self.progress_label.pack(pady=2)
        self.progress_bar.pack(fill="x", padx=20, pady=2)
        thread = threading.Thread(target=self.run_fetch)
        thread.daemon = True
        thread.start()

    def run_fetch(self):
        """Background-thread entry point. Wraps the real work so that ANY exception
        is logged and the UI is reset to a usable state instead of silently hanging
        on "Fetching…" forever (this thread has no Tk event loop to recover it).
        See the icon_to_set/tuple hazard in find_games for the bug this guards."""
        try:
            self._run_fetch_body()
        except Exception as e:
            import traceback
            self.log(f"Fetch failed: {e}", icon="error")
            if fg.DEBUG:
                traceback.print_exc()
            # Reset the UI from this background thread via the _ui marshaling helper.
            self._ui(lambda: self.fetch_button.config(state="normal", text="Fetch Missing Artwork"))
            self._ui(lambda: self.progress_label.pack_forget())
            self._ui(lambda: self.progress_bar.pack_forget())
            self._ui(lambda: self._set_status("Fetch failed — see log."))

    def _run_fetch_body(self):
        """Background thread: search and download artwork for all games missing it."""
        self.last_fetch_files = []
        prefs = load_prefs()
        needs_art = [g for g in self.games if not g["has_art"]]
        if not needs_art:
            self.log("All games already have artwork!", icon="applied")
            self._ui(lambda: self.fetch_button.config(state="disabled", text="Nothing to fetch"))
            self._ui(lambda: self.progress_label.pack_forget())
            self._ui(lambda: self.progress_bar.pack_forget())
            return

        total = len(needs_art)
        fetch_results = []
        # Icon writes to shortcuts.vdf are deferred during the loop and batched into a
        # single atomic write after it (one read+parse+write instead of N), done while
        # Steam is closed so it can't clobber them. {unsigned_id: icon_path}.
        icons_to_set = {}

        for i, game in enumerate(needs_art):
            name   = game["name"]
            app_id = game["app_id"]
            short  = name[:32]
            game_start = i / total * 100
            game_end   = (i + 1) / total * 100

            self.log(f"\nProcessing: {name}", icon="game")
            self._ui(lambda name=name: self._set_status(f"Searching: {name}"))
            self._ui(lambda game_start=game_start: self.progress_var.set(game_start))
            self._ui(lambda i=i, total=total, short=short:
                     self.progress_label.config(text=f"[{i+1}/{total}] {short} — Searching SteamGridDB..."))

            sgdb_id = search_game(name, app_id)
            if sgdb_id is False:
                self.log("Network error — will retry on next fetch", icon="error")
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
                    progress_cb=make_cb(short, game_start, game_end, i, total),
                    defer_icon=True,
                )
                self.log(f"Artwork saved for {name}", icon="applied")
                # Collect the deferred icon write (if any) for one batched apply later.
                # NOTE: use the find_games helpers — results mixes slot dicts with the
                # icon_to_set TUPLE, so a naive values()-loop with .get() crashes here
                # (the bug that silently hung this thread). See find_games helpers.
                icon_pair = fg.icon_write_from_results(results)
                if icon_pair:
                    uid, icon_path = icon_pair
                    icons_to_set[uid] = icon_path
                fetch_results.append({"name": name, "app_id": app_id, "results": results})
                self.last_fetch_files.extend(fg.applied_paths_from_results(results))
            else:
                self.log("Not found on SteamGridDB — skipping permanently", icon="warning")
                add_to_skip_list(app_id)

        # --- Batched icon write -------------------------------------------------
        # Only ICON writes (shortcuts.vdf) care about Steam being open; grid/hero/logo
        # art already wrote to fg.GRID_FOLDER and is fine. "Defer & auto-apply": if Steam
        # is running we DON'T prompt or write — we persist the pending icons and let the
        # poll flush them when Steam closes. WHY: the old from-thread yes/no dialog could
        # hang, and the stop→sleep→write→start restart was racy (Steam could clobber the file).
        if icons_to_set:
            if fg.is_steam_running():
                fg.save_pending_icons(icons_to_set)
                self.log(f"Steam is running — {len(icons_to_set)} icon(s) queued; "
                         "they'll apply automatically when you close Steam.", icon="warning")
            else:
                n = fg.set_shortcut_icons(icons_to_set)
                self.log(f"Applied {n} icon(s) to shortcuts.vdf.", icon="applied")

        self._ui(lambda: self.progress_var.set(100))
        self._ui(lambda n=len(fetch_results), total=total:
                 self.progress_label.config(text=f"Done! Fetched artwork for {n} of {total} game(s)."))

        if fetch_results:
            self.log("\nDone! Opening results screen...", icon="applied")
            self._ui(lambda: self.undo_button.config(state="normal"))
        else:
            self.log("\nDone! No new artwork was fetched.", icon="applied")

        self._ui(lambda: self._set_status("Done!"))
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
        # Build the whole window hidden, then size/position it and only reveal it
        # once — so the user never sees it appear at a default spot and then jump
        # to its final geometry (the "resize flash").
        results_window.withdraw()
        results_window.update_idletasks()
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

        # Steam status reminder (with inline Restart Steam when Steam is running).
        # Rebuilt live by _refresh_results_steam_ui (see below) every ~3s.
        steam_status_frame = tk.Frame(results_window, bg=t["bg"])
        steam_status_frame.pack(pady=2)

        # Pending-icon warning + action sit just ABOVE the bottom buttons so they're
        # next to Back / All Done. Only shown when icons are queued (Steam was open
        # during the fetch). Rebuilt live by _refresh_results_steam_ui.
        action_frame = tk.Frame(results_window, bg=t["bg"])

        # Buttons are pinned to the bottom FIRST so capping the window height can
        # never push them off-screen.
        btn_frame = tk.Frame(results_window, bg=t["bg"])
        # Loader sits in its OWN row UNDER the button row. With side="bottom" the
        # FIRST-packed bottom widget lands LOWEST, so to get the visible top-to-bottom
        # order action_frame -> btn_frame -> loader_frame we pack them bottom-up:
        # loader_frame first (lowest), then btn_frame, then action_frame (highest).
        # The loader is packed ONCE here (always present) — never pack_forget/re-pack
        # during the flow (that on-demand bottom-pack was the invisible-loader bug).
        # Its inner label is empty when idle, so this row is ~0 height and unobtrusive.
        loader_frame = tk.Frame(results_window, bg=t["bg"])
        loader_frame.pack(side="bottom")
        btn_frame.pack(side="bottom", pady=15)
        action_frame.pack(side="bottom", pady=(0, 4))
        def _close_results():
            self._close_modal(results_window)
            results_window.destroy()

        back_btn = self._btn(btn_frame, "← Back", _close_results,
                  font=("Arial", 12), width=12)
        back_btn.pack(side="left", padx=10)
        done_btn = self._btn(btn_frame, "All Done!",
                  lambda: [self._close_modal(results_window),
                           results_window.destroy(), self.window.destroy()],
                  font=("Arial", 13, "bold"), width=12)
        done_btn.pack(side="left", padx=10)
        results_window.protocol("WM_DELETE_WINDOW", _close_results)

        # Per-results-window registry of apply buttons left in the "Queued (close
        # Steam)" state. Reset fresh each time the window opens. _mark_queued_icons_applied
        # flips them to "Applied!" once the queued icons are actually written.
        self._queued_apply_btns = []

        # Remember the live-UI widgets so _refresh_results_steam_ui and the poll can
        # rebuild the status line + pending action on demand without re-deriving them.
        self._results_widgets = {
            "window": results_window, "steam_status_frame": steam_status_frame,
            "action_frame": action_frame, "btn_frame": btn_frame,
            "loader_frame": loader_frame, "back_btn": back_btn, "done_btn": done_btn,
            "t": t,
        }
        # True while the "Close Steam & Apply Icons" flow is mid-run (buttons disabled,
        # its own loader showing); the poll/refresh must NOT rebuild over it then.
        self._results_applying = False
        # Reset the cached render-state for this fresh window so the first poll reconciles
        # against an unknown baseline (the initial build below is rendered directly).
        self._results_steam_state = None
        # Build the status line + (if needed) the pending action for the first time,
        # then start the ~3s poll that keeps them in sync with Steam's live state.
        self._refresh_results_steam_ui()
        self._poll_results_steam_ui()

        canvas, scrollable_frame = self._make_scrollable_frame(results_window, padx=10, pady=5)

        for game_result in fetch_results:
            self.build_game_result_section(scrollable_frame, game_result)

        self._bind_wheel_to_canvas(canvas, canvas)

        # Size the window so the first game section is fully visible. Pin the canvas
        # to the first section's height so tkinter's natural window size accounts
        # for the headers, the first results card and the bottom buttons. Since the
        # buttons are packed at the bottom FIRST, capping the window to the work
        # area shrinks the canvas (not the buttons) and extra sections scroll.
        results_window.update_idletasks()
        sections = scrollable_frame.winfo_children()
        if sections:
            first_section_h = sections[0].winfo_reqheight() + 24
            canvas.config(height=first_section_h)
        # Use the classic 900x750 as the preferred size. Snap the window fully
        # into the visible work area (screen minus the taskbar/panels): keep
        # 900x750 when it fits, otherwise shrink to the work area and let the
        # canvas scroll, always clamping every edge on-screen so the title bar
        # and bottom buttons are never hidden behind a taskbar/panel.
        self._fit_window_to_workarea(results_window, desired_w=900, desired_h=750)
        # Reveal it now that it's correctly sized and positioned — appears once,
        # in place, with no visible resize — then grab it modal over the main window.
        results_window.deiconify()
        self._open_modal(results_window)

    def _refresh_results_steam_ui(self):
        """Re-render the results window's Steam-status line and pending-icon action to
        match the LIVE state (fg.is_steam_running / fg.has_pending_icons). Safe to call
        repeatedly: it tears down and rebuilds the two frames' children each time rather
        than appending, so widgets never stack up. NO-OPs while the apply flow is running
        (it owns those widgets then) or once the window is gone."""
        w = getattr(self, "_results_widgets", None)
        if not w:
            return
        rw = w["window"]
        # Window closed (Back/All Done) -> nothing to refresh.
        try:
            if not rw.winfo_exists():
                return
        except Exception:
            return
        # The "Close Steam & Apply Icons" flow disables buttons + shows its own loader
        # and mutates these same frames; don't fight it mid-run.
        if self._results_applying:
            return

        t = w["t"]
        steam_status_frame = w["steam_status_frame"]
        action_frame = w["action_frame"]

        pending_count = len(fg.load_pending_icons())
        pending_active = pending_count > 0
        steam_running_now = fg.is_steam_running()

        # Only rebuild when the observable state actually changes — otherwise the 3s
        # poll would destroy+recreate the status line and action button every tick,
        # flickering them (and yanking the button out from under the cursor).
        # The COUNT is part of the key (not just has_pending): queuing a 2nd/3rd icon
        # keeps (running, has_pending) unchanged, so a bool key left the "N icon(s)…"
        # label stuck at its first value even though the queue grew.
        state = (steam_running_now, pending_count)
        if getattr(self, "_results_steam_state", None) == state:
            return
        self._results_steam_state = state

        # The silent 4s background poll may have applied the queued icons (user closed
        # Steam manually, no button). If the queue just cleared but we still have buttons
        # stuck on "Queued (close Steam)", flip them to "Applied!" here too.
        if not pending_active and getattr(self, "_queued_apply_btns", None):
            self._mark_queued_icons_applied()

        # --- Status line: rebuild from scratch so it can't accumulate labels. ---
        for child in steam_status_frame.winfo_children():
            child.destroy()
        # Pending icons (queued because Steam was open) need Steam CLOSED to write.
        # When some are pending AND Steam is running, the plain "Restart Steam" button
        # is redundant/misleading — it wouldn't apply them — so we suppress it and let
        # the dedicated "Close Steam & Apply Icons" action handle it instead.
        if steam_running_now:
            sr_lbl = tk.Label(steam_status_frame,
                      text=" Steam is running — restart Steam to see your new artwork",
                      font=("Arial", 9), fg=t["warn"], bg=t["bg"])
            self._iconify(sr_lbl, "warning", self.ICON_INLINE, compound="left")
            sr_lbl.pack(side="left")
            if not pending_active:
                rs_btn = self._btn(steam_status_frame, "Restart Steam", lambda: None, font=("Arial", 8))
                rs_btn.config(command=lambda: [restart_steam(),
                                               rs_btn.config(text="Restarting...", state="disabled")])
                rs_btn.pack(side="left", padx=6)
        else:
            sn_lbl = tk.Label(steam_status_frame,
                      text=" Steam is not running — launch Steam to see your new artwork",
                      font=("Arial", 9), fg=t["muted2"], bg=t["bg"])
            self._iconify(sn_lbl, "offline", self.ICON_INLINE, compound="left")
            sn_lbl.pack(side="left")

        # --- Pending action: show when icons are queued, clear it otherwise. ---
        # Always wipe the frame first so a rebuild can't double up the warning/button.
        for child in action_frame.winfo_children():
            child.destroy()
        if pending_active:
            self._add_pending_icon_action(
                rw, action_frame, steam_status_frame,
                w["btn_frame"], w["loader_frame"], w["back_btn"], w["done_btn"], t)

    def _poll_results_steam_ui(self):
        """~3s timer: refresh the results window's Steam-status + pending UI, then
        reschedule — but only while the window still exists, so closing it (Back/All
        Done) cleanly ends the poll. Only REFRESHES UI; it never applies icons (the
        user drives that via the button; the separate 4s _poll_pending_icons auto-applies)."""
        w = getattr(self, "_results_widgets", None)
        rw = w["window"] if w else None
        try:
            if rw is None or not rw.winfo_exists():
                return  # window gone -> stop (don't reschedule)
        except Exception:
            return
        self._refresh_results_steam_ui()
        rw.after(3000, self._poll_results_steam_ui)

    def _add_pending_icon_action(self, results_window, action_frame, steam_status_frame,
                                 btn_frame, loader_frame, back_btn, done_btn, t):
        """Build the "Close Steam & Apply Icons" warning + button and wire its flow.

        WHY this exists: icons can't be written to shortcuts.vdf while Steam is open
        (Steam clobbers the file on exit), so they were queued. Applying them means
        close Steam → VERIFY it's really gone → write → confirm. We do that on a
        daemon worker thread and marshal every widget update back to the UI thread via
        results_window.after(0, ...). NO modal/blocking dialog and NO blind fixed sleep:
        a modal popped from a worker is exactly what hung the app before, and a fixed
        sleep can't actually confirm Steam closed — so we POLL is_steam_running(force=True)
        instead (force= bypasses the 2.5s cache that would otherwise report a stale True)."""
        pending_count = len(fg.load_pending_icons())
        warn_lbl = tk.Label(action_frame,
                  text=f" {pending_count} icon(s) need Steam closed to apply.",
                  font=("Arial", 9, "bold"), fg=t["warn"], bg=t["bg"])
        self._iconify(warn_lbl, "warning", self.ICON_INLINE, compound="left")
        warn_lbl.pack(side="left", padx=(0, 8))
        action_btn = self._btn(action_frame, "Close Steam & Apply Icons",
                               lambda: None, font=("Arial", 9, "bold"))
        action_btn.pack(side="left")

        # Animated text loader (cycles "Working ." / ".." / "...") in its own row UNDER
        # the Back / All Done buttons. A plain Label avoids any ttk-theme mismatch.
        # Rebuilt fresh each time this action is (re)created, so clear any stale child.
        for child in loader_frame.winfo_children():
            child.destroy()
        loader_lbl = tk.Label(loader_frame, text="", font=("Arial", 10),
                              fg=t["muted2"], bg=t["bg"])
        loader_lbl.pack()
        anim = {"on": False, "step": 0}

        def alive():
            try:
                return bool(results_window.winfo_exists())
            except Exception:
                return False

        def animate():
            # Self-rescheduling dot animation; stops when anim["on"] is cleared.
            if not anim["on"] or not alive():
                return
            dots = "." * (anim["step"] % 3 + 1)
            try:
                loader_lbl.config(text=f"  {anim['base']} {dots}")
            except Exception:
                return
            anim["step"] += 1
            results_window.after(400, animate)

        def set_loader(text):
            # Update the loader's base text (called from the UI thread).
            if not alive():
                return
            anim["base"] = text

        def show_loader(text):
            # The loader_frame is packed ONCE at window creation (always present), so
            # we only start the animation here — never pack/unpack the frame (that was
            # the invisible-loader bug). The empty -> non-empty label simply grows the
            # already-placed row under the buttons.
            anim["base"] = text
            anim["on"] = True
            anim["step"] = 0
            animate()

        def hide_loader():
            # Stop the animation and blank the label so the row collapses to ~0 height
            # again. Do NOT pack_forget the frame — it stays put.
            anim["on"] = False
            if alive():
                try:
                    loader_lbl.config(text="")
                except Exception:
                    pass

        def enable_buttons(include_action):
            if not alive():
                return
            try:
                back_btn.config(state="normal")
                done_btn.config(state="normal")
                if include_action and action_btn.winfo_exists():
                    action_btn.config(state="normal")
            except Exception:
                pass

        def on_success(n):
            # Flow finished — let the poll refresh again. The action/loader stay as the
            # success message below; the next poll sees no pending icons and clears them.
            self._results_applying = False
            hide_loader()
            enable_buttons(include_action=False)
            # Icons are now written — flip any "Queued (close Steam)" buttons to "Applied!".
            self._mark_queued_icons_applied()
            try:
                action_btn.destroy()
                warn_lbl.config(
                    text=(f" Applied {n} icon(s) — launch Steam to see them."
                          if n > 0 else " Icons applied — launch Steam to see them."),
                    fg=t["ok"])
                self._iconify(warn_lbl, "applied", self.ICON_INLINE, compound="left")
                # Steam is now closed — replace the status line accordingly.
                for child in steam_status_frame.winfo_children():
                    child.destroy()
                sn_lbl = tk.Label(steam_status_frame,
                          text=" Steam is not running — launch Steam to see your new artwork",
                          font=("Arial", 9), fg=t["muted2"], bg=t["bg"])
                self._iconify(sn_lbl, "offline", self.ICON_INLINE, compound="left")
                sn_lbl.pack(side="left")
            except Exception:
                pass
            self.log(f"Applied {n} queued icon(s) — launch Steam to see them.",
                     icon="applied")
            # Sync the cached render-state to what we just drew so the poll treats this
            # terminal message as current and won't rebuild over it until state changes.
            self._results_steam_state = (fg.is_steam_running(), len(fg.load_pending_icons()))

        def on_fail(msg):
            # Flow ended (failed) — clear the guard so the poll resumes refreshing.
            self._results_applying = False
            hide_loader()
            enable_buttons(include_action=True)  # let them retry
            if alive():
                try:
                    warn_lbl.config(text=f" {msg}", fg=t["danger"])
                    self._iconify(warn_lbl, "warning", self.ICON_INLINE, compound="left")
                except Exception:
                    pass
            # Pin the cached state to the live values so the poll preserves this error
            # message until something actually changes (e.g. the user closes Steam).
            self._results_steam_state = (fg.is_steam_running(), len(fg.load_pending_icons()))

        def worker():
            # Runs OFF the UI thread. All widget writes go back via results_window.after.
            fg.stop_steam()
            results_window.after(0, lambda: set_loader("Closing Steam"))
            # Poll until Steam is REALLY gone (force=True bypasses the stale cache),
            # up to ~20s. No fixed sleep — we must confirm before writing.
            deadline = time.monotonic() + 20.0
            closed = not fg.is_steam_running(force=True)
            while not closed and time.monotonic() < deadline:
                time.sleep(1.5)
                closed = not fg.is_steam_running(force=True)
            if not closed:
                results_window.after(0, lambda:
                    on_fail("Couldn't close Steam — close it manually and try again."))
                return
            results_window.after(0, lambda: set_loader("Applying icons"))
            n = fg.apply_pending_icons()
            # The silent 4s poll may have won the race and applied them already, so
            # treat an empty queue as success even if our own call wrote 0.
            if n > 0 or not fg.has_pending_icons():
                results_window.after(0, lambda: on_success(n))
            else:
                results_window.after(0, lambda:
                    on_fail("Couldn't apply icons — see the log."))

        def start_apply():
            # UI thread: lock everything, show the loader, then hand off to the worker.
            # Guard the poll/refresh off while we own these widgets (cleared in
            # on_success / on_fail).
            self._results_applying = True
            back_btn.config(state="disabled")
            done_btn.config(state="disabled")
            action_btn.config(state="disabled")
            show_loader("Closing Steam")
            threading.Thread(target=worker, daemon=True).start()

        action_btn.config(command=start_apply)

    def _mark_queued_icons_applied(self):
        """Flip every per-image apply button still showing "Queued (close Steam)" to the
        normal "Applied!" state, once the queued icons have actually been written. Called
        from the apply flow's on_success AND from _refresh_results_steam_ui when the
        pending queue clears via the silent background poll (user closed Steam manually)."""
        btns = getattr(self, "_queued_apply_btns", None)
        if not btns:
            return
        for btn in btns:
            try:
                if btn.winfo_exists():
                    self._set_apply_btn(btn, True)
            except Exception:
                pass
        self._queued_apply_btns = []

    def build_game_result_section(self, parent, game_result):
        """Build the artwork review UI for a single game in the results screen."""
        t = self.theme
        name    = game_result["name"]
        app_id  = game_result["app_id"]
        results = game_result["results"]
        frame = tk.LabelFrame(parent, text=f" {name}", font=("Arial", 12, "bold"),
                               padx=10, pady=10, bg=t["bg"], fg=t["fg"])
        frame.pack(fill="x", padx=10, pady=8)

        art_labels = {"grids": "Cover", "grids_wide": "Wide Cover",
                      "heroes": "Hero/Background", "logos": "Logo", "icons": "Icon"}
        for art_type, label in art_labels.items():
            art_data = results.get(art_type)
            if not art_data:
                continue

            # Each art type gets its own titled card so it's obvious which
            # controls belong to which artwork. The card title is the art-type
            # name (Cover / Wide Cover / ...), replacing the old inline label.
            cell = tk.LabelFrame(frame, text=f" {label} ", font=("Arial", 10, "bold"),
                                  relief="groove", bd=1, padx=8, pady=8,
                                  bg=t["bg"], fg=t["fg"])
            cell.pack(fill="x", pady=6)
            # 3-column grid: col0 = ◀ (fixed), col1 = image (weight=1 so it
            # expands and the thumbnail stays dead-centered between the arrows),
            # col2 = ▶ (fixed). Rows: 0 = arrows+image, 1 = counter, 2 = apply.
            # Weighting only the middle column keeps the arrows pinned to the
            # edges and the image centered at any window width — no screen-pixel
            # assumptions.
            cell.grid_columnconfigure(0, weight=0)
            cell.grid_columnconfigure(1, weight=1)
            cell.grid_columnconfigure(2, weight=0)

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

            # Fixed-size container so the badge can be positioned absolutely on
            # top. The image box itself stays fixed (the thumbnail is pre-scaled
            # to ~300x200), but it sits in the weighted middle column so it stays
            # dead-centered between the ◀/▶ arrows at any window width.
            img_frame = tk.Frame(cell, width=300, height=200, bg=t["placeholder"])
            img_frame.pack_propagate(False)
            img_frame.grid(row=0, column=1, pady=(0, 6))
            img_label = tk.Label(img_frame, bg=t["placeholder"], fg=t["fg"],
                                  text="loading...", wraplength=200)
            img_label.place(x=0, y=0, relwidth=1, relheight=1)
            badge_label = tk.Label(img_frame, text="", bg="#111111", fg="white",
                                    font=("Arial", 11), padx=3, pady=1)
            state["badge_label"] = badge_label

            # Small centered counter on its own row, spanning the full card width
            # so it doesn't break the ◀ image ▶ symmetry above it.
            counter_label = tk.Label(cell, text=f"1 / {len(art_data['option_urls'])}",
                                      font=("Arial", 9), bg=t["bg"], fg=t["fg"])
            counter_label.grid(row=1, column=0, columnspan=3, pady=(0, 6))

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
            # Spans all three columns and stretches (sticky="ew") so its left/right
            # edges align with the ◀ and ▶ arrows above — no fixed width to fight it.
            apply_btn = self._btn(cell, "Apply this one", lambda: None)
            self._set_apply_btn(apply_btn, state["applied_index"] == 0)
            apply_btn.grid(row=2, column=0, columnspan=3, sticky="ew")

            # Clicking the image or badge opens the full-size version in the browser
            for w in (img_frame, img_label, badge_label):
                w.bind("<Button-1>", lambda e, s=state: webbrowser.open(s["option_urls"][s["index"]]))
                w.config(cursor="hand2")

            def make_callbacks(s, il, cl, ab):
                def prev():
                    if s["index"] > 0:
                        s["index"] -= 1
                        update_view(s, il, cl)
                        self._set_apply_btn(ab, s["index"] == s["applied_index"])
                def nxt():
                    if s["index"] < len(s["option_urls"]) - 1:
                        s["index"] += 1
                        update_view(s, il, cl)
                        self._set_apply_btn(ab, s["index"] == s["applied_index"])
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
                            # BUT Steam clobbers shortcuts.vdf on exit, so a write while
                            # Steam is running is silently lost. If Steam is up, DEFER:
                            # queue the icon (auto-applies when Steam closes) and surface
                            # the results-window pending action; only write immediately
                            # when Steam is closed. (s["app_id"] is the int uid.)
                            s["applied_index"] = idx
                            if s["art_type"] == "icons" and fg.is_steam_running():
                                fg.save_pending_icons({s["app_id"]: new_path})
                                self._set_apply_btn(ab, True, queued=True)
                                # Track this queued button so _mark_queued_icons_applied
                                # can flip it to "Applied!" once the icons are written.
                                self._queued_apply_btns.append(ab)
                                # Make the pending warning + "Close Steam & Apply Icons"
                                # action (re)appear/refresh right away.
                                self._refresh_results_steam_ui()
                            else:
                                if s["art_type"] == "icons":
                                    set_shortcut_icon(s["app_id"], new_path)
                                self._set_apply_btn(ab, True)
                    except Exception:
                        ab.config(text="Failed — retry", image="", compound="none")
                return prev, nxt, apply

            prev_fn, next_fn, apply_fn = make_callbacks(state, img_label, counter_label, apply_btn)
            apply_btn.config(command=apply_fn)
            prev_b = self._btn(cell, "◀", prev_fn)
            self._iconify(prev_b, "prev", self.ICON_NAV)
            prev_b.grid(row=0, column=0, padx=4, pady=(0, 6))
            next_b = self._btn(cell, "▶", next_fn)
            self._iconify(next_b, "next", self.ICON_NAV)
            next_b.grid(row=0, column=2, padx=4, pady=(0, 6))

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
        # Nest this popup modally ON TOP of the results window; closing it restores
        # the results window's grab (a bare grab_release would drop modality).
        self._open_modal(popup)

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
                self._close_modal(popup)
                popup.destroy()
            if ok:
                state["applied_index"] = index
                self._set_apply_btn(apply_btn, True)
                apply_btn.config(state="normal")
            else:
                apply_btn.config(text="Failed — retry", image="", compound="none", state="normal")

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
    if meta.get("nsfw"):     parts.append("18+")
    if meta.get("humor"):    parts.append("MEME")
    text = " ".join(parts)
    badge_label.config(text=text)
    if text:
        badge_label.place(x=4, y=4)
    else:
        badge_label.place_forget()


def _display_image_on_label(lbl, path, on_animated=None):
    """Display the image at path on lbl. Plays frame-by-frame if the image is animated.
    on_animated(bool) is called with the actual animation status detected from the file.

    PIL work (Image.open + thumbnail) happens on a worker thread; ImageTk.PhotoImage
    and all widget mutations happen back on the UI thread (Tk requirement).

    Staleness guard: a token is stamped on the label before the worker starts; if the
    token has been replaced by the time the worker finishes (user moved to a different
    image), the result is silently discarded.
    """
    # Cancel any in-flight animation — this runs on the UI thread (safe).
    if hasattr(lbl, "_anim_id") and lbl._anim_id:
        try:
            lbl.after_cancel(lbl._anim_id)
        except Exception:
            pass
        lbl._anim_id = None

    # Stamp a new load token so stale decode results self-discard.
    token = object()
    lbl._load_token = token

    def worker():
        try:
            img = Image.open(path)
            is_animated = getattr(img, "n_frames", 1) > 1

            if is_animated:
                # Image object is opened in the worker; frame iteration starts on the
                # UI thread (via after) because _run_animation calls lbl.after internally.
                def apply_animated():
                    if lbl._load_token is not token:
                        return  # stale — a newer image won the race
                    if on_animated:
                        on_animated(True)
                    _run_animation(lbl, img, 0)
                lbl.after(0, apply_animated)
            else:
                img.thumbnail((300, 200))
                # Copy so the original can be GC'd; thumbnail mutates in-place.
                copy = img.copy()

                def apply_static():
                    if lbl._load_token is not token:
                        return  # stale
                    if on_animated:
                        on_animated(False)
                    photo = ImageTk.PhotoImage(copy)
                    lbl.config(image=photo, text="",
                               width=photo.width(), height=photo.height())
                    lbl.image = photo  # keep a reference so GC doesn't collect it
                lbl.after(0, apply_static)
        except Exception:
            def apply_error():
                if lbl._load_token is not token:
                    return
                lbl.config(text="preview\nunavailable")
            lbl.after(0, apply_error)

    threading.Thread(target=worker, daemon=True).start()


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
