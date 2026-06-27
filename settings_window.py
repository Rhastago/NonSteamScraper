"""The Settings window for NonSteamScraper: API key, appearance (light/dark + accent),
cache, artwork reset, factory reset, update check, Steam status/restart, and the
multi-account selector. One function, open_settings(app); SteamArtApp keeps a thin
wrapper so `self.open_settings()` call sites are unchanged. All shared helpers
(themed widgets, work-area fit, modal stack, logging) are reached through `app`."""

import threading
import webbrowser
import tkinter as tk
from tkinter import messagebox

from appcommon import VERSION
from theming import ACCENTS, PAD_XS, PAD_S, FONT_UI, FONT_MONO
import find_games as fg
from find_games import (
    load_api_key, save_api_key, verify_api_key, get_cache_size, clear_cache,
    clear_managed_artwork, full_reset, is_steam_running, get_all_steam_users,
)


def open_settings(app):
    """Open the settings panel."""
    t = app.theme
    settings = tk.Toplevel(app.window)
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

    tk.Label(settings, text="Settings", font=(FONT_UI, 16, "bold"),
              bg=t["bg"], fg=t["fg"]).pack(pady=10)

    def close_settings():
        app._close_modal(settings)
        settings.destroy()

    # Close button pinned at the bottom so it is always reachable
    app._btn(settings, "Close", close_settings, font=(FONT_UI, 11)).pack(side="bottom", pady=12)
    settings.protocol("WM_DELETE_WINDOW", close_settings)

    # Scrollable body
    canvas, body = app._make_scrollable_frame(settings, padx=5)

    # API Key
    api_frame = tk.LabelFrame(body, text="SteamGridDB API Key", padx=10, pady=8,
                               bg=t["bg"], fg=t["link"])
    api_frame.pack(fill="x", padx=15, pady=5)
    link_frame = tk.Frame(api_frame, bg=t["bg"])
    link_frame.pack(fill="x", pady=2)
    tk.Label(link_frame, text="Get your free key at ", font=(FONT_UI, 9),
              bg=t["bg"], fg=t["fg"]).pack(side="left")
    link = tk.Label(link_frame, text="SteamGridDB", font=(FONT_UI, 9, "underline"),
                    fg=t["link"], bg=t["bg"], cursor="hand2")
    link.pack(side="left")
    link.bind("<Button-1>", lambda e: webbrowser.open(
        "https://www.steamgriddb.com/profile/preferences/api"))
    key_row = tk.Frame(api_frame, bg=t["bg"])
    key_row.pack(fill="x", pady=4)
    api_var = tk.StringVar(value=load_api_key())
    api_entry = tk.Entry(key_row, textvariable=api_var, font=(FONT_MONO, 10), show="*",
                          width=24, bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"],
                          highlightthickness=1, highlightbackground=t["entry_bg"],
                          highlightcolor=t["accent"])
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
    eye_btn = app._btn(btn_holder, "👁",
              lambda: api_entry.config(show="" if api_entry.cget("show") == "*" else "*"),
              font=(FONT_UI, 10))
    app._iconify(eye_btn, "eye", app.ICON_BTN)
    eye_btn.grid(row=0, column=0, sticky="nsew", padx=(0, 2))

    # Status line shows whether a verified key is on file
    key_status = tk.Label(api_frame, text="", font=(FONT_UI, 9), bg=t["bg"], anchor="w")
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
                        app.log("API key verified and saved.", icon="key")
                        app._refresh_onboarding()
                    else:
                        key_status.config(
                            text="✗ Key invalid or unreachable — not saved", fg=t["danger"])
                except Exception:
                    pass
            app.window.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    save_btn = app._btn(btn_holder, "Save", do_save_key, primary=True, font=(FONT_UI, 9))
    save_btn.grid(row=0, column=1, sticky="nsew")

    # Appearance — mode (light/dark) on the first row, accent swatches below.
    appearance_frame = tk.LabelFrame(body, text="Appearance", padx=10, pady=8,
                                      bg=t["bg"], fg=t["link"])
    appearance_frame.pack(fill="x", padx=15, pady=5)

    mode_row = tk.Frame(appearance_frame, bg=t["bg"])
    mode_row.pack(fill="x")
    tk.Label(mode_row, text=f"Current: {app.theme_name.capitalize()} mode",
              font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"]).pack(side="left")
    toggle_text = "Switch to Dark Mode" if app.theme_name == "light" else "Switch to Light Mode"
    app._btn(mode_row, toggle_text, app.toggle_theme,
              font=(FONT_UI, 9)).pack(side="right")

    accent_row = tk.Frame(appearance_frame, bg=t["bg"])
    accent_row.pack(fill="x", pady=(PAD_S, 0))
    tk.Label(accent_row, text="Accent:", font=(FONT_UI, 10),
             bg=t["bg"], fg=t["fg"]).pack(side="left")
    for name, acc in ACCENTS.items():
        selected = (name == app.accent_name)
        # A color swatch button; the active accent gets a bright ring + check.
        sw = tk.Button(
            accent_row, text=("✓" if selected else ""), width=2,
            font=(FONT_UI, 10, "bold"),
            bg=acc["accent"], fg=acc["accent_fg"],
            activebackground=acc["accent_hover"], activeforeground=acc["accent_fg"],
            relief="solid" if selected else "flat",
            bd=2 if selected else 0,
            highlightbackground=t["fg"] if selected else t["bg"],
            highlightthickness=2 if selected else 0,
            cursor="hand2",
            command=lambda n=name: app.set_accent(n))
        sw.pack(side="left", padx=PAD_XS)
        app._add_tooltip(sw, acc["label"] + (" (current)" if selected else ""))

    # Cache
    cache_frame = tk.LabelFrame(body, text="Cache", padx=10, pady=8,
                                 bg=t["bg"], fg=t["link"])
    cache_frame.pack(fill="x", padx=15, pady=5)
    cache_label = tk.Label(cache_frame, text=f"Cache size: {get_cache_size()} MB",
                            font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"])
    cache_label.pack(side="left")

    def do_clear_cache():
        clear_cache()
        cache_label.config(text="Cache size: 0.0 MB")
        app.log("Cache cleared.", icon="trash")

    app._btn(cache_frame, "Clear Cache", do_clear_cache, font=(FONT_UI, 9)).pack(side="right")

    # Artwork
    artwork_frame = tk.LabelFrame(body, text="Artwork", padx=10, pady=8,
                                   bg=t["bg"], fg=t["link"])
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
            app.log(f"Cleared {deleted} artwork file(s) added by NonSteamScraper.",
                     icon="trash")
            app.load_games()
            close_settings()

    tk.Button(artwork_frame, text="Clear All Artwork", font=(FONT_UI, 9),
               fg=t["danger"], bg=t["button_bg"], activebackground=t["select_bg"],
               command=do_clear_artwork).pack(side="left")
    tk.Label(artwork_frame, text="Only removes artwork added by this app",
              font=(FONT_UI, 8), fg=t["muted2"], bg=t["bg"]).pack(side="left", padx=8)

    # Reset
    reset_frame = tk.LabelFrame(body, text="Reset", padx=10, pady=8,
                                 bg=t["bg"], fg=t["link"])
    reset_frame.pack(fill="x", padx=15, pady=5)

    def do_full_reset():
        if messagebox.askyesno(
            "Reset App",
            "This resets the app to a fresh first-launch state.\n\n"
            "It will delete:\n"
            "• Your API key\n"
            "• All preferences and settings\n"
            "• The skip list and name overrides\n"
            "• The thumbnail cache\n\n"
            "Your fetched/applied artwork is KEPT — use 'Clear All Artwork'\n"
            "above if you also want to remove that.\n\n"
            "The app will restart as if it were the first launch.\n\n"
            "Are you sure?",
            parent=settings
        ):
            full_reset()
            app.relaunch_app()

    tk.Button(reset_frame, text="Reset App to Factory Defaults", font=(FONT_UI, 9),
               fg=t["danger"], bg=t["button_bg"], activebackground=t["select_bg"],
               command=do_full_reset).pack(side="left")
    tk.Label(reset_frame, text="Cannot be undone",
              font=(FONT_UI, 8), fg=t["muted2"], bg=t["bg"]).pack(side="left", padx=8)

    # Updates
    updates_frame = tk.LabelFrame(body, text="Updates", padx=10, pady=8,
                                   bg=t["bg"], fg=t["link"])
    updates_frame.pack(fill="x", padx=15, pady=5)
    tk.Label(updates_frame, text=f"Current version: v{VERSION}",
              font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"]).pack(anchor="w")
    update_status = tk.Label(updates_frame, text="", font=(FONT_UI, 9),
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
                        app._btn(
                            download_btn_holder,
                            f"Download v{result['latest']}",
                            lambda url=result["url"]: webbrowser.open(url),
                            font=(FONT_UI, 9),
                        ).pack(side="left")
                    else:
                        update_status.config(
                            text=f"You're on the latest version (v{result['current']}).",
                            fg=t["muted2"])
                except Exception:
                    pass
            app.window.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    check_btn = app._btn(updates_frame, "Check for updates", do_check_update,
                           font=(FONT_UI, 9))
    check_btn.pack(anchor="w", pady=(4, 0))
    # Always-available manual fallback — works even when the auto-check is
    # rate-limited or offline. Same hyperlink style as the SteamGridDB link.
    releases_link = tk.Label(updates_frame, text="View releases page",
                             font=(FONT_UI, 9, "underline"),
                             fg=t["link"], bg=t["bg"], cursor="hand2")
    releases_link.pack(anchor="w", pady=(4, 0))
    releases_link.bind("<Button-1>", lambda e: webbrowser.open(fg.RELEASES_URL))

    # Steam — status + restart. Kept directly above Steam Account so the two
    # Steam-related sections sit together at the bottom (Status above Account).
    steam_frame = tk.LabelFrame(body, text="Steam", padx=10, pady=8,
                                 bg=t["bg"], fg=t["link"])
    steam_frame.pack(fill="x", padx=15, pady=5)
    running = is_steam_running()
    tk.Label(steam_frame, text="Status:", font=(FONT_UI, 10),
              bg=t["bg"], fg=t["fg"]).pack(side="left")
    steam_status_lbl = tk.Label(steam_frame,
              text=" Running" if running else " Not running",
              font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"])
    app._iconify(steam_status_lbl, "online" if running else "offline", app.ICON_INLINE, compound="left")
    steam_status_lbl.pack(side="left", padx=2)
    app._btn(steam_frame, "Restart Steam",
              lambda: [app._restart_steam_async(), close_settings(),
                       app.log("Steam restarting...", icon="refresh")],
              font=(FONT_UI, 9)).pack(side="right")

    # Steam Account — always shown; interactive only when 2+ accounts found.
    # Placed after Updates (under the Steam status section) at the bottom.
    users = get_all_steam_users()
    account_frame = tk.LabelFrame(body, text="Steam Account", padx=10, pady=8,
                                   bg=t["bg"], fg=t["link"])
    account_frame.pack(fill="x", padx=15, pady=5)
    tk.Label(account_frame, text="Select account:", font=(FONT_UI, 10),
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
                app.log(f"Switched to Steam account {label}.", icon="refresh")
                app.load_games()  # reload shortcuts for the newly active account

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

    app._bind_wheel_to_canvas(canvas, canvas)

    # Auto-size to fit all content, fully within the screen work area (so it
    # can't open under a taskbar/panel) — same shared placement helper the
    # main and results windows use.
    settings.update_idletasks()
    body_h  = body.winfo_reqheight()
    overhead = 110  # title label + close button + padding
    body_w = body.winfo_reqwidth() + 40  # +40 for scrollbar and frame padding
    app._fit_window_to_workarea(settings, max(480, body_w), max(360, body_h + overhead))
    # Reveal now that it's correctly sized and positioned, then grab it modal.
    settings.deiconify()
    app._open_modal(settings)
