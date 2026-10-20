"""What the app DOES, with no tkinter: fetch a list of games, re-fetch one game,
re-match one game to a SteamGridDB entry, apply one results option, restore a
hidden game. The UI files (fetch_mixin, library_mixin, dialogs, results_window)
call these and only draw what comes back.

Every rule that changes files lives here or below (appfiles, steam_env, sgdb,
steam_files, restore, artwork), never in a UI
file, so it is written once and tested without a display. Nothing here touches a
widget: run_fetch reports through the callbacks it is given, and the caller
marshals them onto the Tk thread.

Those modules are read live (`module.X` at call time), never imported by value:
an account switch rebinds steam_env.GRID_FOLDER / steam_env.SHORTCUTS_PATH.
"""

import time

from appcommon import debug as _debug
import artwork
import restore
import steam_files
import sgdb
import steam_env
import appfiles


# --- One game ------------------------------------------------------------------

def restore_from_skip(app_id):
    """Hidden → main list: take the game off the skip list. Its art is untouched."""
    appfiles.remove_from_skip_list(app_id)


def ignore(app_id):
    """Ignore: hide a game from fetching. It joins the skip list that "not found on
    SteamGridDB" uses, so Restore brings back either kind. Its art is untouched."""
    appfiles.add_to_skip_list(app_id)


def refetch(app_id):
    """Re-fetch: take the game off the skip list and clear its art so the next
    fetch downloads it again. clear_game_art snapshots first (Undo can put the art
    back) and deletes only this id's files. Returns the number of files removed."""
    appfiles.remove_from_skip_list(app_id)
    return restore.clear_game_art(app_id)


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
        return artwork.install_option(save_path, url, unsigned_id, is_icon=is_icon,
                                 animated=animated, progress_cb=progress_cb,
                                 status_cb=status_cb)
    except Exception:
        _debug("apply_option")
        return False, False


# --- Queued icons: "Close Steam & Apply Icons" -------------------------------------

# close_steam_and_apply_icons outcomes.
ICONS_APPLIED      = "applied"        # written (n may be 0: the poll got there first)
ICONS_STEAM_STAYED = "steam_stayed"   # Steam was still running after the wait
ICONS_NOT_WRITTEN  = "not_written"    # the shortcuts.vdf write failed; the queue stays


def close_steam_and_apply_icons(on_step=None, wait=20.0, interval=1.5):
    """The results window's "Close Steam & Apply Icons": stop Steam, wait until it
    has REALLY exited (a fixed sleep cannot confirm that, and the cached
    is_steam_running would report a stale True, hence force=True), then write the
    queued icons. Runs on a worker thread; on_step(text) names each stage. Returns
    (outcome, n): ICONS_APPLIED with the count written, ICONS_STEAM_STAYED, or
    ICONS_NOT_WRITTEN (the write failed and the queue is kept for a retry)."""
    steam_env.stop_steam()
    if on_step:
        on_step("Closing Steam")
    deadline = time.monotonic() + wait
    closed = not steam_env.is_steam_running(force=True)
    while not closed and time.monotonic() < deadline:
        time.sleep(interval)
        closed = not steam_env.is_steam_running(force=True)
    if not closed:
        return ICONS_STEAM_STAYED, 0
    if on_step:
        on_step("Applying icons")
    n = steam_files.apply_pending_icons()
    if n is None:
        return ICONS_NOT_WRITTEN, 0
    # The 4 s poll may have flushed the queue first; an empty queue is success
    # even when this call wrote 0.
    if n > 0 or not steam_files.has_pending_icons():
        return ICONS_APPLIED, n
    return ICONS_NOT_WRITTEN, 0


# --- A fetch run ---------------------------------------------------------------

# run_fetch outcomes (report["outcome"]).
FETCH_NOTHING     = "nothing"       # no game needed art; nothing ran
FETCH_REJECTED    = "rejected"      # SGDB refused the key before the run; nothing ran
FETCH_UNREACHABLE = "unreachable"   # SGDB could not be asked; nothing ran
FETCH_RAN         = "ran"           # the run happened (to the end, or stopped)

UNREACHABLE_MESSAGE = ("Couldn't reach SteamGridDB — nothing was fetched. "
                       "Check your connection and try again.")


def _fetch_one(game, i, total, prefs, picked_ids, on_progress, on_log):
    """One game of run_fetch: find its SteamGridDB id (the one picked in Search →
    Select, else a search), then download its art. Returns (outcome, FetchedArt or
    None): outcome "refused" stops the run; a network error, a genuine "not found"
    (skip-listed — the only outcome that hides a game) and success go on. May raise:
    run_fetch catches it and skips only this game."""
    name, app_id = game["name"], game["app_id"]
    if str(app_id) in picked_ids:
        outcome, sgdb_id = sgdb.SEARCH_FOUND, picked_ids[str(app_id)]
        on_log(f"  Using the SteamGridDB match you picked (#{sgdb_id})", None)
    else:
        outcome, sgdb_id = sgdb.search_game(name, app_id)
    if outcome == sgdb.SEARCH_REFUSED:
        return "refused", None
    if outcome == sgdb.SEARCH_ERROR:
        on_log("Network error — will retry on next fetch", "error")
        return "error", None
    if outcome != sgdb.SEARCH_FOUND:
        # Only a genuine "no results" (SEARCH_NOT_FOUND) reaches here.
        on_log("Not found on SteamGridDB — skipping permanently", "warning")
        appfiles.add_to_skip_list(app_id)
        return "not_found", None
    start_pct, end_pct = i / total * 100, (i + 1) / total * 100

    def slot_progress(label, step, total_steps):
        pct = start_pct + (step / total_steps) * (end_pct - start_pct)
        on_progress(pct, f"[{i+1}/{total}] {name[:32]} — {label}")

    return "found", artwork.download_all_artwork(sgdb_id, app_id, prefs,
                                                 progress_cb=slot_progress,
                                                 defer_icon=True)


def _log_game_outcome(name, elapsed, art, on_log):
    """The log line for one fetched game. Never says "saved" for a slot whose
    download failed (B5) or that SteamGridDB did not answer (N6): it names them, and
    their previous art was kept."""
    failed, unanswered = art.failed_labels(), art.unanswered
    if not failed and not unanswered:
        on_log(f"Artwork saved for {name} ({elapsed:.1f}s)", "applied")
        return
    parts = []
    if failed:
        parts.append(f"download failed for {', '.join(failed)}")
    if unanswered:
        parts.append(f"SteamGridDB did not answer for {', '.join(unanswered)}")
    detail = "; ".join(parts)
    if art.applied_paths():
        on_log(f"Artwork saved for {name} ({elapsed:.1f}s) — {detail}; the previous "
               "art was kept", "warning")
    elif not unanswered:
        on_log(f"Download failed for {name} ({elapsed:.1f}s) — "
               f"{', '.join(failed)}; the previous art was kept", "error")
    else:
        on_log(f"No artwork saved for {name} ({elapsed:.1f}s) — {detail}; the "
               "previous art was kept. Fetch again later.", "error")


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

    key_state = sgdb.check_api_key()
    if key_state == sgdb.KEY_REJECTED:
        on_log("SteamGridDB rejected your API key — nothing was fetched.", "error")
        report["outcome"] = FETCH_REJECTED
        return report
    if key_state != sgdb.KEY_ACCEPTED:
        on_log(UNREACHABLE_MESSAGE, "error")
        report["outcome"] = FETCH_UNREACHABLE
        return report

    report["outcome"] = FETCH_RAN
    restore.begin_restore_run()
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
        on_log(f"\nProcessing: {name}", "game")
        on_progress(i / total * 100,
                    f"[{i+1}/{total}] {name[:32]} — Searching SteamGridDB...",
                    f"Searching: {name}")
        try:
            outcome, art = _fetch_one(game, i, total, prefs, picked_ids, on_progress, on_log)
        except Exception as e:
            # N10: one malformed SteamGridDB record or a failed registry write used
            # to abort the whole loop — no seal, and the icons of the games already
            # done never reached shortcuts.vdf. Now only this game is skipped.
            _debug("run_fetch game")
            on_log(f"Something went wrong with {name} ({e}) — skipped; its previous "
                   "art was kept.", "error")
            continue
        if outcome == "refused":
            # Safety net for a key revoked after the check: stop, hide nothing.
            on_log("SteamGridDB rejected the API key — fetch stopped. "
                   "No games were hidden.", "error")
            report["stopped"] = "Fetch stopped — SteamGridDB rejected the API key."
            break
        if art is None:
            continue
        elapsed = time.monotonic() - start
        _log_game_outcome(name, elapsed, art, on_log)
        if art.icon_write:
            uid, icon_path = art.icon_write
            icons_to_set[uid] = icon_path
        fetch_results.append({"name": name, "app_id": app_id, "results": art})

    # End of the run: seal every entry — the user has accepted this run's art.
    restore.seal_restore_point()

    if icons_to_set:
        n, queued = steam_files.write_or_queue_icons(icons_to_set)
        if n is None:
            on_log(f"Couldn't write {len(icons_to_set)} icon(s) to shortcuts.vdf — "
                   "they are queued and will be retried automatically.", "warning")
        elif queued:
            on_log(f"Steam is running — {len(icons_to_set)} icon(s) queued; "
                   "they'll apply automatically when you close Steam.", "warning")
        else:
            on_log(f"Applied {n} icon(s) to shortcuts.vdf.", "applied")

    head = "Stopped —" if report["stopped"] else "Done!"
    on_progress(100, f"{head} Fetched artwork for {len(fetch_results)} of {total} game(s).")
    if fetch_results:
        on_log("\nDone! Opening results screen...", "applied")
    else:
        on_log("\nDone! No new artwork was fetched.", "applied")
    return report
