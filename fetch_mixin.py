"""The fetch pipeline + activity log + deferred-icon handling for NonSteamScraper:
starting and running the background fetch, appending to the (thread-safe) log,
checking/restarting Steam, undoing the last fetch, and the pending-icon
flush/poll that auto-applies queued icons once Steam closes. Mixed into SteamArtApp.

The fetch itself is operations.run_fetch (no tkinter); this mixin starts it on a
daemon thread and draws what it reports. Every widget update is marshalled back
onto the Tk thread via self._ui / self.window.after."""

import threading
from tkinter import messagebox

from appcommon import DEBUG
from theming import PAD_XS, PAD_L
import find_games as fg
import steam_files
import steam_env
import operations
from steam_env import is_steam_running, restart_steam
from appfiles import load_api_key, load_prefs

class FetchMixin:

    # The status line a write command shows while a fetch runs (Q3).
    BUSY_MESSAGE = "Fetch in progress — try again when it finishes."

    def _refuse_if_busy(self):
        """Tk thread: True — and say so on the status line — while a fetch runs.
        Every command that writes to disk calls this first (B2): a second fetch, an
        account switch, a reset or a relaunch in the middle of a run writes into the
        files the running fetch is writing."""
        if self._fetch_busy:
            self._set_status(self.BUSY_MESSAGE, self.theme["warn"], icon="warning")
            return True
        return False

    def check_steam_running(self):
        """Display a warning if Steam is open, since artwork changes require a restart."""
        # Opportunistic flush: a manual refresh after closing Steam applies queued icons
        # immediately (the periodic poll is the primary mechanism).
        self._flush_pending_icons()
        if is_steam_running():
            self._set_status(
                "Steam is running — restart Steam after fetching to see changes",
                self.theme["warn"], icon="warning")


    def undo_fetch(self):
        """Restore the artwork that was present before the last fetch or Re-fetch,
        delegating the work to find_games.undo_restore_point (deletion, restore,
        icon writes). Runs on the Tk thread — it is a button command — so the
        confirmation messagebox is safe here. Undo is destructive and cannot itself
        be undone, and its state now comes from disk, so when the restore point was
        inherited from a previous session and no fetch has run since, confirm first."""
        if self._refuse_if_busy():
            return
        if not fg.has_restore_point():
            self.log("Nothing to undo.", icon="warning")
            return
        if self._restore_point_inherited and not self._fetched_this_session:
            # Count comes from the live manifest (fg. reads rebound globals).
            count = fg.restore_point_game_count()
            if not messagebox.askyesno(
                "Undo Last Fetch",
                f"Undo will restore the artwork for {count} game(s) from a "
                "restore point saved by a previous session.\n\n"
                "Undo is destructive and cannot itself be undone.\n\n"
                "Are you sure?",
                parent=self.window
            ):
                return
        summary = fg.undo_restore_point()
        if summary["games"] == 0:
            self.log("Nothing to undo.", icon="warning")
            return
        queued = " Icons are queued and will apply when Steam closes." if summary["queued"] else ""
        self.log(f"Undo complete — restored {summary['files']} file(s) for "
                 f"{summary['games']} game(s), removed {summary['removed']} file(s)."
                 f"{queued}", icon="undo")
        self.load_games()


    def _restart_steam_async(self):
        """Run restart_steam() (stop → sleep(3) → start) off the UI thread so the ~3s
        wait never freezes the window. Fire-and-forget daemon thread."""
        threading.Thread(target=restart_steam, daemon=True).start()


    def _flush_pending_icons(self):
        """Auto-apply deferred icon writes when it's safe. No-op (cheap existence check)
        when nothing is pending or Steam is running. Steam is closed when this writes, so
        the icons show on Steam's next launch. Part of the "defer & auto-apply" design:
        no dialog, no Steam restart by us (see find_games.PENDING_ICONS_FILE)."""
        # Skip while the results window's explicit "Close Steam & Apply Icons" flow is
        # mid-run — it applies the queue itself; letting the poll also fire is harmless
        # (idempotent) but this avoids the two racing over the same file.
        view = self._results_view
        if view is not None and view.is_applying():
            return
        if steam_files.has_pending_icons() and not steam_env.is_steam_running():
            n = steam_files.apply_pending_icons()
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
            # Hint that there's new activity while the drawer is collapsed.
            if not getattr(self, "_log_visible", True):
                self.log_toggle.config(text="▶  Details  •")
            self.window.update_idletasks()
        self.window.after(0, append)


    def start_fetch(self, games=None):
        """Disable the fetch button, show progress UI, and start the fetch thread.
        games: the game dicts to fetch (Search → Select passes the one matched game);
        None fetches every game missing art. Does nothing while a fetch runs."""
        if self._fetch_busy:
            return
        if not load_api_key():
            messagebox.showinfo(
                "API Key Required",
                "You need a free SteamGridDB API key before fetching artwork.\n\n"
                "Open Settings to add your key, or click the info (ℹ) button for help.",
                parent=self.window
            )
            self.open_settings()
            return

        # Busy from here until _end_fetch, which every worker exit path reaches.
        self._fetch_busy = True
        self._fetch_cancel = threading.Event()
        # What a stopped run may leave behind, for _finish_close's .part cleanup.
        targets = games if games is not None else [g for g in self.games if not g["has_art"]]
        self._fetch_run = {"grid": steam_env.GRID_FOLDER, "ids": [g["app_id"] for g in targets]}
        self.fetch_button.config(state="disabled", text="Fetching...")
        self.undo_button.config(state="disabled")
        self._set_status("Working...")
        self.progress_var.set(0)
        self.progress_label.pack(pady=PAD_XS, before=self.log_toggle)
        self.progress_bar.pack(fill="x", padx=PAD_L, pady=PAD_XS,
                               before=self.log_toggle)
        # Make sure the just-added progress widgets are visible (grow the window
        # if needed) rather than getting pushed off the bottom.
        self._refit_window_height()
        thread = threading.Thread(target=self.run_fetch, args=(games,))
        thread.daemon = True
        thread.start()


    def run_fetch(self, games=None):
        """Background-thread entry point. Wraps the real work so that ANY exception
        is logged and the UI is reset to a usable state instead of silently hanging
        on "Fetching…" forever (this thread has no Tk event loop to recover it).
        The finally block is the ONE way out: whatever happened, _end_fetch runs on
        the Tk thread and clears the busy state."""
        fetch_results = None
        try:
            fetch_results = self._run_fetch_body(games)
        except Exception as e:
            import traceback
            self.log(f"Fetch failed: {e}", icon="error")
            if DEBUG:
                traceback.print_exc()
            self._reset_fetch_ui("Fetch failed — see log.")
        finally:
            self._ui(lambda r=fetch_results: self._end_fetch(r))


    def _end_fetch(self, fetch_results=None):
        """Tk thread: the fetch worker has exited — at the end of the run, early
        (nothing to fetch, key refused, SGDB unreachable) or by an exception. Clear
        the busy state FIRST, so load_games re-derives Fetch and Undo instead of
        keeping them disabled. fetch_results is None unless the run reached its end.
        If the window is closing (Q4), close it now instead."""
        self._fetch_busy = False
        if self._closing:
            self._fetch_followup = None
            self._finish_close()
            return
        self._fetch_run = None
        # A Settings window opened during the run gets its write controls back (S2).
        release, self._settings_on_fetch_end = self._settings_on_fetch_end, None
        if release:
            release()
        self.fetch_button.config(state="normal", text="Fetch Missing Artwork")
        self.load_games()
        # A step the run asked for once it is over (the rejected-key popup, which
        # opens Settings — so it must come after the busy state has cleared).
        followup, self._fetch_followup = self._fetch_followup, None
        if followup:
            followup()
        if fetch_results is not None:
            self.check_steam_running()
            if fetch_results:
                self.show_results(fetch_results)


    def _reset_fetch_ui(self, status, color=None, icon=None):
        """Worker thread: put the window back to idle after a run that ended
        without results — Fetch enabled, progress hidden, `status` on the status
        line. Every widget call is marshalled through self._ui. Fetch itself is
        re-enabled by _end_fetch."""
        self._ui(lambda: self.progress_label.pack_forget())
        self._ui(lambda: self.progress_bar.pack_forget())
        self._ui(lambda: self._refit_window_height())
        self._ui(lambda: self._set_status(status, color, icon=icon))


    def _show_key_rejected(self):
        """Tk thread: SteamGridDB refused the saved key before a fetch, so nothing
        ran. Say so, then open Settings — the same pattern as the no-key popup."""
        messagebox.showwarning(
            "API Key Rejected",
            "SteamGridDB rejected your API key, so nothing was fetched.\n\n"
            "Open Settings to check or replace it.",
            parent=self.window
        )
        self.open_settings()


    def _run_fetch_body(self, games=None):
        """Background thread: run operations.run_fetch for `games`, or for every
        game missing art when games is None, and draw what it reports. Returns the
        per-game results when the run happened, None when nothing ran."""
        if games is not None:
            needs_art = list(games)
        else:
            needs_art = [g for g in self.games if not g["has_art"]]

        def on_progress(pct, label, status=None):
            if status is not None:
                self._ui(lambda status=status: self._set_status(status))
            self._ui(lambda pct=pct: self.progress_var.set(pct))
            self._ui(lambda label=label: self.progress_label.config(text=label))

        def on_log(message, icon):
            self.log(message, icon=icon)

        def should_stop():
            # Closing the window during a run sets this (Q4): stop between games.
            return self._fetch_cancel is not None and self._fetch_cancel.is_set()

        report = operations.run_fetch(needs_art, load_prefs(), on_progress, on_log,
                                      should_stop)
        outcome = report["outcome"]
        if outcome == operations.FETCH_NOTHING:
            self._ui(lambda: self.fetch_button.config(state="disabled", text="Nothing to fetch"))
            self._ui(lambda: self.progress_label.pack_forget())
            self._ui(lambda: self.progress_bar.pack_forget())
            self._ui(lambda: self._refit_window_height())
            return None
        if outcome == operations.FETCH_REJECTED:
            self._reset_fetch_ui("API key rejected — nothing was fetched.",
                                 self.theme["warn"], icon="warning")
            self._fetch_followup = self._show_key_rejected  # run by _end_fetch
            return None
        if outcome == operations.FETCH_UNREACHABLE:
            self._reset_fetch_ui(operations.UNREACHABLE_MESSAGE, self.theme["warn"],
                                 icon="warning")
            return None

        # The run happened: this scopes undo_fetch's inherited-restore-point
        # confirmation to restore points older than this session's fetches.
        self._fetched_this_session = True
        self._ui(lambda stopped=report["stopped"]: self._set_status(stopped or "Done!"))
        # _end_fetch (from run_fetch) reloads the list and opens the results screen.
        return report["results"]
