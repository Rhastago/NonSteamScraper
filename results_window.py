"""The post-fetch Results window for NonSteamScraper: review the applied artwork,
cycle alternatives, apply a different option (converting animated picks to APNG),
and the live Steam-status / pending-icon coordination that lets queued icon writes
apply once Steam closes.

Everything is a function taking the SteamArtApp instance as `app`; SteamArtApp keeps
thin wrapper methods so internal `self._refresh_results_steam_ui()` etc. resolve. The
cross-window coordination flags (app._results_applying, app._results_widgets,
app._queued_apply_btns, app._results_steam_state, app.results_window) live on `app`
so the main window's pending-icon poll (fetch_mixin) stays in sync."""

import time
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk
import requests

from theming import FONT_UI
import find_games as fg
from find_games import register_managed_file, set_shortcut_icon, clear_slot_files, download_apng
from imaging import _update_badge, _display_image_on_label, update_view


def show_results(app, fetch_results):
    """Open the results window to review and swap applied artwork."""
    t = app.theme
    results_window = tk.Toplevel(app.window)
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
    results_window.transient(app.window)
    # Remembered so the animated-conversion popup can sit directly on top of it.
    app.results_window = results_window

    tk.Label(results_window, text="Review Applied Artwork",
              font=(FONT_UI, 16, "bold"), bg=t["bg"], fg=t["fg"]).pack(pady=10)
    tk.Label(results_window, text="Click an image to view it fully sized.",
              font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"]).pack()
    tk.Label(results_window, text="Cycle through alternatives and apply your preferred art.",
              font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"]).pack()

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
        app._close_modal(results_window)
        results_window.destroy()

    back_btn = app._btn(btn_frame, "← Back", _close_results,
              font=(FONT_UI, 12), width=12)
    back_btn.pack(side="left", padx=10)
    done_btn = app._btn(btn_frame, "All Done!",
              lambda: [app._close_modal(results_window),
                       results_window.destroy(), app.window.destroy()],
              font=(FONT_UI, 13, "bold"), width=12)
    done_btn.pack(side="left", padx=10)
    results_window.protocol("WM_DELETE_WINDOW", _close_results)

    # Per-results-window registry of apply buttons left in the "Queued (close
    # Steam)" state. Reset fresh each time the window opens. _mark_queued_icons_applied
    # flips them to "Applied!" once the queued icons are actually written.
    app._queued_apply_btns = []

    # Remember the live-UI widgets so _refresh_results_steam_ui and the poll can
    # rebuild the status line + pending action on demand without re-deriving them.
    app._results_widgets = {
        "window": results_window, "steam_status_frame": steam_status_frame,
        "action_frame": action_frame, "btn_frame": btn_frame,
        "loader_frame": loader_frame, "back_btn": back_btn, "done_btn": done_btn,
        "t": t,
    }
    # True while the "Close Steam & Apply Icons" flow is mid-run (buttons disabled,
    # its own loader showing); the poll/refresh must NOT rebuild over it then.
    app._results_applying = False
    # Reset the cached render-state for this fresh window so the first poll reconciles
    # against an unknown baseline (the initial build below is rendered directly).
    app._results_steam_state = None
    # Build the status line + (if needed) the pending action for the first time,
    # then start the ~3s poll that keeps them in sync with Steam's live state.
    _refresh_results_steam_ui(app)
    _poll_results_steam_ui(app)

    canvas, scrollable_frame = app._make_scrollable_frame(results_window, padx=10, pady=5)

    # One-time snapshot of the pending-icon queue so each game's build can tell
    # whether its auto-applied icon was actually written or merely QUEUED (Steam
    # was open during the fetch). Read once here, not per-game, to avoid N file reads.
    app._build_pending_icons = fg.load_pending_icons()

    for game_result in fetch_results:
        build_game_result_section(app, scrollable_frame, game_result)

    app._bind_wheel_to_canvas(canvas, canvas)

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
    app._fit_window_to_workarea(results_window, desired_w=900, desired_h=750)
    # Reveal it now that it's correctly sized and positioned — appears once,
    # in place, with no visible resize — then grab it modal over the main window.
    results_window.deiconify()
    app._open_modal(results_window)


def _refresh_results_steam_ui(app):
    """Re-render the results window's Steam-status line and pending-icon action to
    match the LIVE state (fg.is_steam_running / fg.has_pending_icons). Safe to call
    repeatedly: it tears down and rebuilds the two frames' children each time rather
    than appending, so widgets never stack up. NO-OPs while the apply flow is running
    (it owns those widgets then) or once the window is gone."""
    w = getattr(app, "_results_widgets", None)
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
    if app._results_applying:
        return

    t = w["t"]
    steam_status_frame = w["steam_status_frame"]
    action_frame = w["action_frame"]

    # has_pending_icons() is a cheap existence check; only open+JSON-parse the queue
    # file when something is actually pending (this runs every ~3s poll tick). The
    # count must stay correct when pending — it feeds the state key and the "N icon(s)…"
    # label — so we keep the full parse on the has-pending path.
    pending_count = len(fg.load_pending_icons()) if fg.has_pending_icons() else 0
    pending_active = pending_count > 0
    steam_running_now = fg.is_steam_running()

    # Only rebuild when the observable state actually changes — otherwise the 3s
    # poll would destroy+recreate the status line and action button every tick,
    # flickering them (and yanking the button out from under the cursor).
    # The COUNT is part of the key (not just has_pending): queuing a 2nd/3rd icon
    # keeps (running, has_pending) unchanged, so a bool key left the "N icon(s)…"
    # label stuck at its first value even though the queue grew.
    state = (steam_running_now, pending_count)
    if getattr(app, "_results_steam_state", None) == state:
        return
    app._results_steam_state = state

    # The silent 4s background poll may have applied the queued icons (user closed
    # Steam manually, no button). If the queue just cleared but we still have buttons
    # stuck on "Queued (close Steam)", flip them to "Applied!" here too.
    if not pending_active and getattr(app, "_queued_apply_btns", None):
        _mark_queued_icons_applied(app)

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
                  font=(FONT_UI, 9), fg=t["warn"], bg=t["bg"])
        app._iconify(sr_lbl, "warning", app.ICON_INLINE, compound="left")
        sr_lbl.pack(side="left")
        if not pending_active:
            rs_btn = app._btn(steam_status_frame, "Restart Steam", lambda: None, font=(FONT_UI, 8))
            rs_btn.config(command=lambda: [app._restart_steam_async(),
                                           rs_btn.config(text="Restarting...", state="disabled")])
            rs_btn.pack(side="left", padx=6)
    else:
        sn_lbl = tk.Label(steam_status_frame,
                  text=" Steam is not running — launch Steam to see your new artwork",
                  font=(FONT_UI, 9), fg=t["muted2"], bg=t["bg"])
        app._iconify(sn_lbl, "offline", app.ICON_INLINE, compound="left")
        sn_lbl.pack(side="left")

    # --- Pending action: show when icons are queued, clear it otherwise. ---
    # Always wipe the frame first so a rebuild can't double up the warning/button.
    for child in action_frame.winfo_children():
        child.destroy()
    if pending_active:
        _add_pending_icon_action(
            app, rw, action_frame, steam_status_frame,
            w["btn_frame"], w["loader_frame"], w["back_btn"], w["done_btn"], t)


def _poll_results_steam_ui(app):
    """~3s timer: refresh the results window's Steam-status + pending UI, then
    reschedule — but only while the window still exists, so closing it (Back/All
    Done) cleanly ends the poll. Only REFRESHES UI; it never applies icons (the
    user drives that via the button; the separate 4s _poll_pending_icons auto-applies)."""
    w = getattr(app, "_results_widgets", None)
    rw = w["window"] if w else None
    try:
        if rw is None or not rw.winfo_exists():
            return  # window gone -> stop (don't reschedule)
    except Exception:
        return
    _refresh_results_steam_ui(app)
    rw.after(3000, lambda: _poll_results_steam_ui(app))


def _add_pending_icon_action(app, results_window, action_frame, steam_status_frame,
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
              font=(FONT_UI, 9, "bold"), fg=t["warn"], bg=t["bg"])
    app._iconify(warn_lbl, "warning", app.ICON_INLINE, compound="left")
    warn_lbl.pack(side="left", padx=(0, 8))
    action_btn = app._btn(action_frame, "Close Steam & Apply Icons",
                           lambda: None, primary=True, font=(FONT_UI, 9, "bold"))
    action_btn.pack(side="left")

    # Animated text loader (cycles "Working ." / ".." / "...") in its own row UNDER
    # the Back / All Done buttons. A plain Label avoids any ttk-theme mismatch.
    # Rebuilt fresh each time this action is (re)created, so clear any stale child.
    for child in loader_frame.winfo_children():
        child.destroy()
    loader_lbl = tk.Label(loader_frame, text="", font=(FONT_UI, 10),
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
        app._results_applying = False
        hide_loader()
        enable_buttons(include_action=False)
        # Icons are now written — flip any "Queued (close Steam)" buttons to "Applied!".
        _mark_queued_icons_applied(app)
        try:
            action_btn.destroy()
            warn_lbl.config(
                text=(f" Applied {n} icon(s) — launch Steam to see them."
                      if n > 0 else " Icons applied — launch Steam to see them."),
                fg=t["ok"])
            app._iconify(warn_lbl, "applied", app.ICON_INLINE, compound="left")
            # Steam is now closed — replace the status line accordingly.
            for child in steam_status_frame.winfo_children():
                child.destroy()
            sn_lbl = tk.Label(steam_status_frame,
                      text=" Steam is not running — launch Steam to see your new artwork",
                      font=(FONT_UI, 9), fg=t["muted2"], bg=t["bg"])
            app._iconify(sn_lbl, "offline", app.ICON_INLINE, compound="left")
            sn_lbl.pack(side="left")
        except Exception:
            pass
        app.log(f"Applied {n} queued icon(s) — launch Steam to see them.",
                 icon="applied")
        # Sync the cached render-state to what we just drew so the poll treats this
        # terminal message as current and won't rebuild over it until state changes.
        app._results_steam_state = (fg.is_steam_running(), len(fg.load_pending_icons()))

    def on_fail(msg):
        # Flow ended (failed) — clear the guard so the poll resumes refreshing.
        app._results_applying = False
        hide_loader()
        enable_buttons(include_action=True)  # let them retry
        if alive():
            try:
                warn_lbl.config(text=f" {msg}", fg=t["danger"])
                app._iconify(warn_lbl, "warning", app.ICON_INLINE, compound="left")
            except Exception:
                pass
        # Pin the cached state to the live values so the poll preserves this error
        # message until something actually changes (e.g. the user closes Steam).
        app._results_steam_state = (fg.is_steam_running(), len(fg.load_pending_icons()))

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
        app._results_applying = True
        back_btn.config(state="disabled")
        done_btn.config(state="disabled")
        action_btn.config(state="disabled")
        show_loader("Closing Steam")
        threading.Thread(target=worker, daemon=True).start()

    action_btn.config(command=start_apply)


def _mark_queued_icons_applied(app):
    """Flip every per-image apply button still showing "Queued (close Steam)" to the
    normal "Applied!" state, once the queued icons have actually been written. Called
    from the apply flow's on_success AND from _refresh_results_steam_ui when the
    pending queue clears via the silent background poll (user closed Steam manually)."""
    entries = getattr(app, "_queued_apply_btns", None)
    if not entries:
        return
    # Each entry carries everything needed to finish the job now that the icon is
    # actually written: promote its queued_index to applied_index, clear the queued
    # marker, and re-sync that slot's button label + border. This is where the
    # "Applied!" label and the accent border finally appear for a deferred icon.
    for entry in entries:
        try:
            s = entry["state"]
            s["applied_index"] = entry["index"]
            s["queued_index"] = None
            btn = entry["btn"]
            if btn.winfo_exists():
                _sync_apply_ui(app, s, btn)
        except Exception:
            pass
    app._queued_apply_btns = []


def _set_applied_border(app, state):
    """Frame the image in the accent color when the option being viewed is the
    one currently applied to Steam; plain otherwise. applied_index is None when
    nothing is applied yet (all-animated slot), so no option is framed then. A
    queued (deferred) icon is NOT applied yet, so it never gets a border — only the
    truly-written applied_index does."""
    fr = state.get("img_frame")
    if fr is None:
        return
    # Now called from several places (incl. after-conversion / deferred writes), so
    # the frame may have been destroyed when the results window closed mid-flight —
    # guard so a TclError can't bubble out of a worker's after-callback.
    try:
        if not fr.winfo_exists():
            return
        if state["applied_index"] is not None and state["index"] == state["applied_index"]:
            fr.config(highlightthickness=3,
                      highlightbackground=app.theme["accent"],
                      highlightcolor=app.theme["accent"])
        else:
            fr.config(highlightthickness=0)
    except Exception:
        pass


def _sync_apply_ui(app, state, apply_btn):
    """Single source of truth for a slot's apply-button label AND accent border,
    derived from the currently-viewed index vs the queued/applied indices.
    queued (deferred icon) wins over applied; border shows only for the truly-applied index."""
    idx = state["index"]
    qi = state.get("queued_index")
    if qi is not None and idx == qi:
        app._set_apply_btn(apply_btn, True, queued=True)
    else:
        ai = state.get("applied_index")
        app._set_apply_btn(apply_btn, ai is not None and idx == ai)
    _set_applied_border(app, state)


def build_game_result_section(app, parent, game_result):
    """Build the artwork review UI for a single game in the results screen."""
    t = app.theme
    name    = game_result["name"]
    app_id  = game_result["app_id"]
    results = game_result["results"]
    frame = tk.LabelFrame(parent, text=f" {name}", font=(FONT_UI, 12, "bold"),
                           padx=10, pady=10, bg=t["bg"], fg=t["link"])
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
        cell = tk.LabelFrame(frame, text=f" {label} ", font=(FONT_UI, 10, "bold"),
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
            # Index whose icon write was DEFERRED (queued because Steam was open).
            # Distinct from applied_index: queued means "intent, not yet written", so
            # it drives the "Queued (close Steam)" label but never the accent border.
            "queued_index": None,
            "art_type": art_type,
            "app_id": app_id,
        }

        # An icon auto-applied during the fetch while Steam was running was NOT
        # written to shortcuts.vdf — the fetch pipeline saved it to the pending
        # queue instead. So its applied_index is intent, not a real write: present
        # it exactly like a manual deferred apply ("Queued (close Steam)", no
        # border) until Steam closes and the queue flushes. Detected via the
        # one-time snapshot taken in show_results (keys are int uids).
        if (art_type == "icons" and state["applied_index"] is not None
                and app_id in getattr(app, "_build_pending_icons", {})):
            state["queued_index"] = state["applied_index"]
            state["applied_index"] = None

        # Fixed-size container so the badge can be positioned absolutely on
        # top. The image box itself stays fixed (the thumbnail is pre-scaled
        # to ~300x200), but it sits in the weighted middle column so it stays
        # dead-centered between the ◀/▶ arrows at any window width.
        img_frame = tk.Frame(cell, width=300, height=200, bg=t["placeholder"])
        img_frame.pack_propagate(False)
        img_frame.grid(row=0, column=1, pady=(0, 6))
        state["img_frame"] = img_frame
        img_label = tk.Label(img_frame, bg=t["placeholder"], fg=t["fg"],
                              text="loading...", wraplength=200)
        img_label.place(x=0, y=0, relwidth=1, relheight=1)
        badge_label = tk.Label(img_frame, text="", bg="#111111", fg="white",
                                font=(FONT_UI, 11), padx=3, pady=1)
        state["badge_label"] = badge_label

        # Small centered counter on its own row, spanning the full card width
        # so it doesn't break the ◀ image ▶ symmetry above it.
        counter_label = tk.Label(cell, text=f"1 / {len(art_data['option_urls'])}",
                                  font=(FONT_UI, 9), bg=t["bg"], fg=t["fg"])
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
        apply_btn = app._btn(cell, "Apply this one", lambda: None, primary=True)
        apply_btn.grid(row=2, column=0, columnspan=3, sticky="ew")
        # Initial button label + border via the single source of truth (#5).
        _sync_apply_ui(app, state, apply_btn)

        # An icon that arrived already-queued from the fetch (above) must be tracked
        # like a manual deferred apply so the same close-Steam flow promotes it to
        # "Applied!" + border once the queue is written.
        if state["queued_index"] is not None:
            app._queued_apply_btns.append(
                {"state": state, "index": state["queued_index"],
                 "btn": apply_btn, "il": img_label, "cl": counter_label})

        # Clicking the image or badge opens the full-size version in the browser
        for w in (img_frame, img_label, badge_label):
            w.bind("<Button-1>", lambda e, s=state: webbrowser.open(s["option_urls"][s["index"]]))
            w.config(cursor="hand2")

        def make_callbacks(s, il, cl, ab):
            # prev/nxt share one body (#5): bounds-check, move ±1, refresh the view,
            # then re-sync the button label + border through the single helper so the
            # border can never be forgotten or drift out of step with the label.
            def step(delta):
                new_idx = s["index"] + delta
                if 0 <= new_idx <= len(s["option_urls"]) - 1:
                    s["index"] = new_idx
                    update_view(s, il, cl)
                    _sync_apply_ui(app, s, ab)
            def prev():
                step(-1)
            def nxt():
                step(1)
            def apply():
                idx = s["index"]
                url = s["option_urls"][idx]
                meta = s["option_meta"][idx] if idx < len(s["option_meta"]) else {}
                base_noext = s["applied_path"].rsplit(".", 1)[0]
                # Animated picks need APNG conversion (Steam ignores webp/gif), which
                # is slow — run it with a progress popup instead of blocking the UI.
                if meta.get("animated"):
                    apply_animated_art(app, s, ab, url, base_noext + ".png", idx)
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
                        if s["art_type"] == "icons" and fg.is_steam_running():
                            # DEFERRED: the icon isn't written yet, so this is intent,
                            # not application. Mark queued_index (NOT applied_index) so
                            # the slot shows "Queued (close Steam)" with NO border; the
                            # border + "Applied!" only land once the write happens.
                            fg.save_pending_icons({s["app_id"]: new_path})
                            s["queued_index"] = idx
                            _sync_apply_ui(app, s, ab)
                            # Track enough to finish the job later: _mark_queued_icons_applied
                            # promotes queued_index -> applied_index and re-syncs this slot
                            # once the icons are actually written.
                            app._queued_apply_btns.append(
                                {"state": s, "index": idx, "btn": ab, "il": il, "cl": cl})
                            # Make the pending warning + "Close Steam & Apply Icons"
                            # action (re)appear/refresh right away.
                            _refresh_results_steam_ui(app)
                        else:
                            # Immediate write succeeded — NOW it's truly applied, so set
                            # applied_index and let _sync_apply_ui draw the border + label.
                            if s["art_type"] == "icons":
                                set_shortcut_icon(s["app_id"], new_path)
                            s["applied_index"] = idx
                            _sync_apply_ui(app, s, ab)
                    else:
                        # stream=True does NOT raise on 4xx/5xx, so the except below
                        # never catches a non-200 — give the same explicit feedback.
                        ab.config(text="Failed — retry", image="", compound="none")
                except Exception:
                    ab.config(text="Failed — retry", image="", compound="none")
            return prev, nxt, apply

        prev_fn, next_fn, apply_fn = make_callbacks(state, img_label, counter_label, apply_btn)
        apply_btn.config(command=apply_fn)
        prev_b = app._btn(cell, "◀", prev_fn)
        app._iconify(prev_b, "prev", app.ICON_NAV)
        prev_b.grid(row=0, column=0, padx=4, pady=(0, 6))
        next_b = app._btn(cell, "▶", next_fn)
        app._iconify(next_b, "next", app.ICON_NAV)
        next_b.grid(row=0, column=2, padx=4, pady=(0, 6))


def apply_animated_art(app, state, apply_btn, url, out_path, index):
    """Apply an animated artwork pick by converting it to APNG (the only animated
    format Steam renders), shown with a modal progress popup. The conversion is
    slow, so it runs on a worker thread and reports progress back to the popup."""
    t = app.theme
    apply_btn.config(text="Converting…", state="disabled")

    # Sit the popup on top of the results window (it's launched from there). Being
    # transient to the results window keeps that window as its master, so the WM
    # raises the results window with the popup instead of pushing it behind the
    # main game-list window.
    parent = getattr(app, "results_window", None)
    if parent is None or not parent.winfo_exists():
        parent = app.window

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
             font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"], wraplength=320).pack(pady=(16, 6))
    status = tk.Label(popup, text="Starting…", font=(FONT_UI, 9),
                      bg=t["bg"], fg=t["muted2"])
    status.pack()
    # Real per-frame progress comes from find_games.download_apng (it counts the
    # APNG frame chunks Pillow writes), so this is an accurate determinate bar.
    try:
        bar = ttk.Progressbar(popup, mode="determinate", maximum=100, length=300,
                              style="Accent.Horizontal.TProgressbar")
    except Exception:
        bar = ttk.Progressbar(popup, mode="determinate", maximum=100, length=300)
    bar.pack(pady=10)
    # Nest this popup modally ON TOP of the results window; closing it restores
    # the results window's grab (a bare grab_release would drop modality).
    app._open_modal(popup)

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
        app.window.after(0, paint)

    def on_status(msg):
        app.window.after(0, lambda m=msg: status.config(text=m)
                          if status.winfo_exists() else None)

    def finish(ok):
        if popup.winfo_exists():
            app._close_modal(popup)
            popup.destroy()
        # The worker marshals this back via after(0,...); if the results window was
        # destroyed mid-conversion, apply_btn / state["img_frame"] are gone, so touching
        # them would raise a TclError. Bail once the popup is closed.
        if not apply_btn.winfo_exists():
            return
        if ok:
            state["applied_index"] = index
            state["queued_index"] = None
            _sync_apply_ui(app, state, apply_btn)
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
        app.window.after(0, lambda: finish(ok))

    threading.Thread(target=worker, daemon=True).start()
