"""What the app DOES, with no tkinter: fetch a list of games, re-fetch one game,
re-match one game to a SteamGridDB entry, apply one results option, restore a
hidden game. The UI files (fetch_mixin, library_mixin, dialogs, results_window)
call these and only draw what comes back.

Every rule that changes files lives here or below (find_games), never in a UI
file, so it is written once and tested without a display. Nothing here touches a
widget: run_fetch reports through the callbacks it is given, and the caller
marshals them onto the Tk thread.

find_games is read live (`fg.X` at call time), never imported by value: an
account switch rebinds fg.GRID_FOLDER / fg.SHORTCUTS_PATH.
"""

import time

from appcommon import debug as _debug
import find_games as fg
import steam_env
import appfiles


# --- One game ------------------------------------------------------------------

def restore_from_skip(app_id):
    """Hidden → main list: take the game off the skip list. Its art is untouched."""
    appfiles.remove_from_skip_list(app_id)


def refetch(app_id):
    """Re-fetch: take the game off the skip list and clear its art so the next
    fetch downloads it again. clear_game_art snapshots first (Undo can put the art
    back) and deletes only this id's files. Returns the number of files removed."""
    appfiles.remove_from_skip_list(app_id)
    return fg.clear_game_art(app_id)


def rematch(app_id, name, sgdb_id):
    """Search → Select: remember the SteamGridDB game the user picked (its id,
    which the next fetch uses without searching again — B8: a re-search by name
    could land on a same-named game — and its name, as before), then clear the
    game's art exactly like Re-fetch. The caller then fetches this one game."""
    appfiles.save_id_override(app_id, sgdb_id)
    appfiles.save_name_override(app_id, name)
    return refetch(app_id)


def apply_option(save_path, url, unsigned_id, is_icon=False, animated=False,
                 progress_cb=None, status_cb=None):
    """The results window's "Apply this one": install the option into its slot and,
    for the icon slot, write shortcuts.vdf now (Steam closed) or queue the write
    (Steam running). Returns (ok, queued). Never raises — a worker thread calls it."""
    try:
        return fg.install_option(save_path, url, unsigned_id, is_icon=is_icon,
                                 animated=animated, progress_cb=progress_cb,
                                 status_cb=status_cb)
    except Exception:
        _debug("apply_option")
        return False, False


# --- A fetch run ---------------------------------------------------------------

# run_fetch outcomes (report["outcome"]).
FETCH_NOTHING     = "nothing"       # no game needed art; nothing ran
FETCH_REJECTED    = "rejected"      # SGDB refused the key before the run; nothing ran
FETCH_UNREACHABLE = "unreachable"   # SGDB could not be asked; nothing ran
FETCH_RAN         = "ran"           # the run happened (to the end, or stopped)

UNREACHABLE_MESSAGE = ("Couldn't reach SteamGridDB — nothing was fetched. "
                       "Check your connection and try again.")


def run_fetch(games, prefs, on_progress, on_log, should_stop):
    """Fetch artwork for `games` (the game dicts to fetch). Runs on a worker thread.

    on_log(message, icon) — one activity-log line.
    on_progress(percent, label, status=None) — the progress bar, its label and
        (when given) the status line.
    should_stop() — True once the run must stop; checked between games.

    Order (each step is load-bearing):
      1. nothing to fetch → return at once (a no-op run must not touch the restore
         point);
      2. the key check BEFORE anything changes on disk — a refused key used to come
         back as "not found" for every game and hide the whole library (B3);
      3. begin_restore_run, then the games; a refusal mid-run stops the loop and
         hides nothing; only a genuine "no results" is skip-listed;
      4. seal_restore_point after the loop, then ONE batched icon write: queued
         while Steam runs (it rewrites shortcuts.vdf on exit), written when closed.

    Returns a report dict: outcome (FETCH_*), results (one {name, app_id,
    results: FetchedArt} per game whose art was fetched), total, stopped (None, or the status
    line saying why the run stopped early)."""
    report = {"outcome": FETCH_NOTHING, "results": [], "total": len(games),
              "stopped": None}
    if not games:
        on_log("All games already have artwork!", "applied")
        return report

    key_state = fg.check_api_key()
    if key_state == fg.KEY_REJECTED:
        on_log("SteamGridDB rejected your API key — nothing was fetched.", "error")
        report["outcome"] = FETCH_REJECTED
        return report
    if key_state != fg.KEY_ACCEPTED:
        on_log(UNREACHABLE_MESSAGE, "error")
        report["outcome"] = FETCH_UNREACHABLE
        return report

    report["outcome"] = FETCH_RAN
    fg.begin_restore_run()
    total = len(games)
    fetch_results = report["results"]
    # Icon writes are collected during the loop and written once after it.
    icons_to_set = {}
    # Games matched by hand in Search → Select: fetch that SGDB id, no search.
    picked_ids = appfiles.load_id_overrides()

    for i, game in enumerate(games):
        if should_stop():
            on_log("Fetch stopped — the window is closing.", "warning")
            report["stopped"] = "Fetch stopped."
            break
        start = time.monotonic()
        name = game["name"]
        app_id = game["app_id"]
        short = name[:32]
        game_start = i / total * 100
        game_end = (i + 1) / total * 100

        on_log(f"\nProcessing: {name}", "game")
        on_progress(game_start, f"[{i+1}/{total}] {short} — Searching SteamGridDB...",
                    f"Searching: {name}")

        if str(app_id) in picked_ids:
            outcome, sgdb_id = fg.SEARCH_FOUND, picked_ids[str(app_id)]
            on_log(f"  Using the SteamGridDB match you picked (#{sgdb_id})", None)
        else:
            outcome, sgdb_id = fg.search_game(name, app_id)
        if outcome == fg.SEARCH_REFUSED:
            # Safety net for a key revoked after the check: stop, hide nothing.
            on_log("SteamGridDB rejected the API key — fetch stopped. "
                   "No games were hidden.", "error")
            report["stopped"] = "Fetch stopped — SteamGridDB rejected the API key."
            break
        if outcome == fg.SEARCH_ERROR:
            on_log("Network error — will retry on next fetch", "error")
            continue
        if outcome != fg.SEARCH_FOUND:
            # Only a genuine "no results" (SEARCH_NOT_FOUND) reaches here.
            on_log("Not found on SteamGridDB — skipping permanently", "warning")
            appfiles.add_to_skip_list(app_id)
            continue

        def slot_progress(label, step, total_steps, _i=i, _short=short,
                          _start=game_start, _end=game_end):
            pct = _start + (step / total_steps) * (_end - _start)
            on_progress(pct, f"[{_i+1}/{total}] {_short} — {label}")

        results = fg.download_all_artwork(sgdb_id, app_id, prefs,
                                          progress_cb=slot_progress, defer_icon=True)
        elapsed = time.monotonic() - start
        # Never say "saved" for a slot whose download failed (B5): name the failed
        # slots, whose previous art was kept.
        failed = results.failed_labels()
        if not failed:
            on_log(f"Artwork saved for {name} ({elapsed:.1f}s)", "applied")
        elif results.applied_paths():
            on_log(f"Artwork saved for {name} ({elapsed:.1f}s) — download failed "
                   f"for {', '.join(failed)}; the previous art was kept", "warning")
        else:
            on_log(f"Download failed for {name} ({elapsed:.1f}s) — "
                   f"{', '.join(failed)}; the previous art was kept", "error")
        if results.icon_write:
            uid, icon_path = results.icon_write
            icons_to_set[uid] = icon_path
        fetch_results.append({"name": name, "app_id": app_id, "results": results})

    # End of the run: seal every entry — the user has accepted this run's art.
    fg.seal_restore_point()

    if icons_to_set:
        if steam_env.is_steam_running():
            fg.save_pending_icons(icons_to_set)
            on_log(f"Steam is running — {len(icons_to_set)} icon(s) queued; "
                   "they'll apply automatically when you close Steam.", "warning")
        else:
            n = fg.set_shortcut_icons(icons_to_set)
            on_log(f"Applied {n} icon(s) to shortcuts.vdf.", "applied")

    head = "Stopped —" if report["stopped"] else "Done!"
    on_progress(100, f"{head} Fetched artwork for {len(fetch_results)} of {total} game(s).")
    if fetch_results:
        on_log("\nDone! Opening results screen...", "applied")
    else:
        on_log("\nDone! No new artwork was fetched.", "applied")
    return report
