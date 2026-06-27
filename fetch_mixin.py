"""The fetch pipeline + activity log + deferred-icon handling for NonSteamScraper:
starting and running the background fetch, appending to the (thread-safe) log,
checking/restarting Steam, undoing the last fetch, and the pending-icon
flush/poll that auto-applies queued icons once Steam closes. Mixed into SteamArtApp.

Threading: run_fetch and friends do their work on daemon threads and marshal every
widget update back onto the Tk thread via self._ui / self.window.after."""

import os
import threading
from tkinter import messagebox

from theming import PAD_XS, PAD_L
import find_games as fg
from find_games import (
    load_api_key, load_prefs, search_game, download_all_artwork,
    add_to_skip_list, is_steam_running, restart_steam,
)

class FetchMixin:

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
        if getattr(self, "_results_applying", False):
            return
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
            # Hint that there's new activity while the drawer is collapsed.
            if not getattr(self, "_log_visible", True):
                self.log_toggle.config(text="▶  Details  •")
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
        self.progress_label.pack(pady=PAD_XS, before=self.log_toggle)
        self.progress_bar.pack(fill="x", padx=PAD_L, pady=PAD_XS,
                               before=self.log_toggle)
        # Make sure the just-added progress widgets are visible (grow the window
        # if needed) rather than getting pushed off the bottom.
        self._refit_window_height()
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
            self._ui(lambda: self._refit_window_height())
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
            self._ui(lambda: self._refit_window_height())
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
