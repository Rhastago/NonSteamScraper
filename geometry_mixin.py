"""Window placement + modal-stack management for NonSteamScraper.

Handles work-area-aware sizing/centering (so windows never open under a taskbar
or panel), the dynamic main-window refit, the application-local modal grab stack,
and window lifecycle (icon, close). The pure size/position math lives in
winplace (compute_window_fit / parse_net_workarea) so it can be unit-tested
without a display; the tkinter side effects stay here. Mixed into SteamArtApp."""

import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk

from appcommon import resource_path, debug as _debug
import artwork
from winplace import parse_net_workarea, compute_window_fit, place_over_parent

class GeometryMixin:

    @staticmethod
    def _safe(fn):
        """Call fn() swallowing any Tk error (window gone / not viewable)."""
        try:
            fn()
        except Exception:
            pass


    # Backoff (ms) between grab attempts on a window that is not viewable yet: about
    # 1.3 s in all, for a compositor that maps windows late (N22).
    GRAB_RETRY_MS = (10, 20, 40, 80, 160, 320, 640)

    def _open_modal(self, win):
        """Make `win` modal over the current chain and push it onto _modal_stack.

        The new top sits transient-above whatever is currently top-of-stack (or the
        main window), so nested popups stack correctly and stay visible-with-parent.
        grab_set() is an application-local grab: it blocks this app's OTHER windows
        (lower in the stack) but not other OS apps. A grab on a withdrawn / not-yet-
        mapped window raises TclError, so callers must deiconify first; when the
        window is still not viewable, the grab is retried when it maps (<Map>) and
        on the GRAB_RETRY_MS backoff, and a log line says so if it never takes (N22:
        one retry after 10 ms, then the dialog silently stayed non-modal)."""
        self._prune_modal_stack()
        parent = self._modal_stack[-1] if self._modal_stack else self.window
        self._safe(lambda: win.transient(parent))
        self._safe(win.update_idletasks)
        grabbed = [False]

        def _try():
            if not grabbed[0]:
                try:
                    win.grab_set()
                    grabbed[0] = True
                except tk.TclError:
                    pass
            return grabbed[0]

        def _retry(attempt):
            try:
                if not win.winfo_exists():
                    return   # closed before it ever became viewable
            except Exception:
                return
            if _try():
                return
            if attempt == len(self.GRAB_RETRY_MS):
                title = ""
                try:
                    title = win.title()
                except Exception:
                    pass
                print(f"Could not make the '{title}' window modal — it is open, but "
                      "the windows behind it still take clicks.")
                return
            try:
                win.after(self.GRAB_RETRY_MS[attempt], lambda: _retry(attempt + 1))
            except Exception:
                pass

        if not _try():
            self._safe(lambda: win.bind("<Map>", lambda e: _try(), add="+"))
            _retry(0)
        self._safe(win.lift)
        self._safe(win.focus_force)
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
        # win and every popup inside it: closing a window destroys its child
        # popups without their own close path running (N13: the results window
        # closed during an animated conversion left the dead progress popup on the
        # stack, and every later dialog chained itself to it and lost its focus).
        inside = str(win) + "."
        self._modal_stack[:] = [w for w in self._modal_stack
                                if w is not win and not str(w).startswith(inside)]
        self._prune_modal_stack()
        if self._modal_stack:
            top = self._modal_stack[-1]
            self._safe(top.grab_set)
            self._safe(top.lift)
            self._safe(top.focus_force)
        else:
            self._safe(self.window.lift)
            self._safe(self.window.focus_force)


    def _prune_modal_stack(self):
        """Drop windows that no longer exist from _modal_stack (N13)."""
        alive = []
        for w in self._modal_stack:
            try:
                if w.winfo_exists():
                    alive.append(w)
            except Exception:
                pass
        self._modal_stack[:] = alive


    def _place_over_main(self, win, desired_w, desired_h):
        """Size `win` and centre it over the main window, kept fully inside the
        screen work area (N18). One geometry() call, as the flash-free reveal
        needs. Returns the (w, h) applied."""
        self.window.update_idletasks()
        wa = self._get_workarea(win)
        w, h, x, y = place_over_parent(
            self.window.winfo_x(), self.window.winfo_y(),
            self.window.winfo_width(), self.window.winfo_height(), *wa,
            desired_w, desired_h)
        win.geometry(f"{w}x{h}+{x}+{y}")
        return w, h


    def _fit_dialog(self, win, canvas=None, body=None, centre_on_screen=False):
        """Size a pop-up window to what its content needs on THIS screen (ruling Q16):
        measured after it is built, never typed — typed sizes clipped the content
        on another display or font size. The scrolling canvas (when given) asks for
        its body's full size first; the result is capped to the work area, so a
        small screen gets a shorter window whose body scrolls. Then the size is
        FIXED (ruling Q16b): under KDE any window whose height can be dragged keeps a
        maximize button and fills the screen (the max-width hint is ignored), and
        these windows have nothing to show in extra space. Centred over the main
        window, or on the screen."""
        win.update_idletasks()
        if canvas is not None and body is not None:
            canvas.config(width=body.winfo_reqwidth(), height=body.winfo_reqheight())
            win.update_idletasks()
        want_w, want_h = win.winfo_reqwidth(), win.winfo_reqheight()
        if centre_on_screen:
            w, h = self._fit_window_to_workarea(win, want_w, want_h)
        else:
            w, h = self._place_over_main(win, want_w, want_h)
        # An older typed minimum must not push the window past a small screen.
        win.minsize(w, h)
        win.resizable(False, False)


    # The widest the main window's content and the Results cards grow (ruling
    # Q17), in pixels at 96 dpi; scaled by the display's font scaling.
    COLUMN_MAX = 1000

    def _column_width(self):
        """COLUMN_MAX for this display: Tk's scaling is 96/72 at 96 dpi."""
        try:
            scaling = float(self.window.tk.call("tk", "scaling"))
        except Exception:
            scaling = 96 / 72
        return int(self.COLUMN_MAX * scaling / (96 / 72))


    def _center_column(self, event):
        """<Configure> of the main window: pad the content column so it is never
        wider than _column_width(), centred (Q17)."""
        if event.widget is not self.window:
            return
        pad = max(0, (event.width - self._column_width()) // 2)
        if pad != getattr(self, "_column_pad", 0):
            self._column_pad = pad
            self._safe(lambda: self.column.pack_configure(padx=pad))


    def _set_window_icon(self):
        """Load the bundled icon for the window and all child windows.
        Silently does nothing if the icon file is missing."""
        try:
            self._icon_image = ImageTk.PhotoImage(Image.open(resource_path("icon.png")))
            self.window.iconphoto(True, self._icon_image)
        except Exception:
            pass


    def _autosize_window(self):
        """Size the window to fit all its content so nothing is clipped on open,
        positioned fully within the screen work area (never under a taskbar/panel)
        and centered. Temporarily packs the progress widgets to include them in
        the measurement, then hides them again so they only appear during a fetch."""
        self._pack_progress()
        self.window.update_idletasks()
        req_w = max(self.window.winfo_reqwidth(), 700)
        req_h = self.window.winfo_reqheight()
        self.progress_label.pack_forget()
        self.progress_bar.pack_forget()
        # Reuse the same work-area-aware fit as the results window so the main
        # window can't open under a taskbar/panel either. Remember the applied
        # size in case it is needed by future clamping logic.
        self._main_w, self._main_h = self._fit_window_to_workarea(
            self.window, req_w, req_h)
        self._update_min_height()


    # How many list rows must stay visible however short the main window is made.
    MIN_LIST_ROWS = 2

    def _update_min_height(self):
        """The main window may not be made shorter than every row except the game
        list, plus MIN_LIST_ROWS list rows: below that a control is squeezed out of
        sight (seen 2026-10-20: Fetch Missing Artwork, with Details open at 520 px).
        Capped to the work area. Re-run whenever a row appears or goes (Details,
        the progress bar), through _refit_window_height."""
        try:
            self.window.update_idletasks()
            fixed = self.column.winfo_reqheight() - self.list_canvas.winfo_reqheight()
            need = fixed + self.MIN_LIST_ROWS * (self.ROW_THUMB_H + 6)
            avail = max(self._get_workarea(self.window)[3] - 80, 200)
            self.window.minsize(560, min(need, avail))
        except Exception:
            _debug("_update_min_height")


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
                # Parsing delegated to the pure winplace helper so it can be
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
        # The pure size+position math lives in winplace.compute_window_fit so it
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


    def _refit_window_height(self):
        """Re-measure the main window's required height and resize it to fit its
        current content, so nothing clips when the Details drawer opens/closes or
        the fetch progress bar appears/disappears.

        Keeps the current WIDTH and the current top-left (x, y). Clamps so the
        window never extends past the work-area bottom: if growing downward would
        overflow, the window shifts UP just enough to fit (but never above the work
        area top), and if the content is taller than the whole available area the
        height is capped to that area so the window never exceeds the screen."""
        self._update_min_height()
        try:
            win = self.window
            win.update_idletasks()
            wa_x, wa_y, wa_w, wa_h = self._get_workarea(win)
            # Available height below the work-area top (leave the same panel/taskbar
            # reserve the initial fit uses, so behavior stays consistent).
            RESERVE = 80
            avail_h = max(wa_h - RESERVE, 200)
            # Keep the current width; only the height is dynamic.
            cur_w = win.winfo_width()
            req_h = win.winfo_reqheight()
            new_h = min(req_h, avail_h)
            # Current top-left.
            x = win.winfo_x()
            y = win.winfo_y()
            # If the bottom edge would fall past the work-area bottom, shift up
            # enough to fit — but never above the work-area top.
            bottom_limit = wa_y + avail_h
            if y + new_h > bottom_limit:
                y = bottom_limit - new_h
            if y < wa_y:
                y = wa_y
            win.geometry(f"{cur_w}x{new_h}+{x}+{y}")
        except Exception:
            pass


    # How long a stopping fetch may keep the window open before it closes anyway
    # (a game stuck on a stalled download must not hold the app open for good).
    CLOSE_FALLBACK_MS = 30000

    def _on_close(self):
        """Window close button. During a fetch, ask first (Q4); on yes, set the run's
        stop flag and let the worker finish the game it is on — _end_fetch then
        closes the window through _finish_close. Closing at once used to kill the
        daemon worker mid-download and leave .part files in Steam's grid folder."""
        if self._apply_targets and not self._fetch_busy:
            # N4: an "Apply this one" is still downloading or converting; closing
            # now would kill it mid-way. Close when it ends (_apply_finished), or
            # after CLOSE_FALLBACK_MS at the latest.
            if self._closing:
                return
            self._closing = True
            self._set_status("Finishing the artwork apply — the window closes when it is done…",
                             self.theme["warn"], icon="warning")
            self.window.after(self.CLOSE_FALLBACK_MS, self._finish_close)
            return
        if self._fetch_busy:
            if self._closing:
                return  # already stopping; the window closes when the run ends
            if not messagebox.askyesno("Fetch in progress",
                                       "A fetch is running — stop and close?",
                                       parent=self.window):
                return
            # The question is modal but the Tk queue keeps running under it: the
            # fetch may have ended (and _end_fetch run, not closing) meanwhile.
            # Close now instead of waiting CLOSE_FALLBACK_MS for an ended run.
            if not self._fetch_busy:
                self._finish_close()
                return
            self._closing = True
            self._fetch_cancel.set()
            self._set_status("Stopping the fetch — the window closes after the current game…",
                             self.theme["warn"], icon="warning")
            self.window.after(self.CLOSE_FALLBACK_MS, self._finish_close)
            return
        self._finish_close()


    def _finish_close(self):
        """Tk thread: close the window, once. After a stopped fetch, first remove
        the .part files that run left behind."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        run = self._fetch_run
        if run:
            artwork.remove_partial_downloads(run["grid"], run["ids"])
        # An apply still running at the fallback leaves its unfinished download.
        for path in list(self._apply_targets):
            artwork.remove_part_file(path)
        # We deliberately do not persist or restore the window position — the window
        # always re-centers on launch — so there is nothing to save here.
        # Stop the thumbnail decode pool so no worker survives the window. Don't wait
        # on in-flight decodes — their UI assignment self-discards once the window is
        # gone (winfo_exists/after both fail safely).
        try:
            self._thumb_executor.shutdown(wait=False)
        except Exception:
            pass
        self.window.destroy()


    def _ui(self, fn):
        """Schedule fn to run on the main (UI) thread. Safe to call from workers."""
        self.window.after(0, fn)
