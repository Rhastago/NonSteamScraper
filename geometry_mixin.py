"""Window placement + modal-stack management for NonSteamScraper.

Handles work-area-aware sizing/centering (so windows never open under a taskbar
or panel), the dynamic main-window refit, the application-local modal grab stack,
and window lifecycle (icon, close). The pure size/position math lives in
find_games (compute_window_fit / parse_net_workarea) so it can be unit-tested
without a display; the tkinter side effects stay here. Mixed into SteamArtApp."""

import tkinter as tk
from PIL import Image, ImageTk

from appcommon import resource_path
from theming import PAD_XS, PAD_L
from find_games import parse_net_workarea, compute_window_fit

class GeometryMixin:

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


    def _autosize_window(self):
        """Size the window to fit all its content so nothing is clipped on open,
        positioned fully within the screen work area (never under a taskbar/panel)
        and centered. Temporarily packs the progress widgets to include them in
        the measurement, then hides them again so they only appear during a fetch."""
        self.progress_label.pack(pady=PAD_XS, before=self.log_toggle)
        self.progress_bar.pack(fill="x", padx=PAD_L, pady=PAD_XS,
                               before=self.log_toggle)
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


    def _refit_window_height(self):
        """Re-measure the main window's required height and resize it to fit its
        current content, so nothing clips when the Details drawer opens/closes or
        the fetch progress bar appears/disappears.

        Keeps the current WIDTH and the current top-left (x, y). Clamps so the
        window never extends past the work-area bottom: if growing downward would
        overflow, the window shifts UP just enough to fit (but never above the work
        area top), and if the content is taller than the whole available area the
        height is capped to that area so the window never exceeds the screen."""
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


    def _restore_geometry(self):
        """No-op: the main window now centers on the work area every launch
        (done by _autosize_window) and intentionally ignores any saved position.
        A saved position could land off-screen after a display change (e.g. the
        Deck docked to an external display, then undocked) with no way to recover
        it, so we always re-center instead. Kept as a method for compatibility."""
        return


    def _on_close(self):
        # We no longer persist the window position — the window always centers on
        # launch (see _restore_geometry) — so there is nothing to save here.
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
