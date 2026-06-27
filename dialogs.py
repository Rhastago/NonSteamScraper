"""Standalone popup windows for NonSteamScraper that aren't big enough to warrant
their own module: the quick-start guide (show_info), the live SteamGridDB search
dialog (open_sgdb_search), and the Art Style Preferences window (open_art_prefs).

Each is a function taking the SteamArtApp instance as `app`; it reaches the shared
themed-widget helpers, modal stack, and logging through `app`. SteamArtApp keeps a
thin wrapper method per function so existing `self.open_*()` call sites are unchanged."""

import os
import glob
import threading
import webbrowser
import tkinter as tk
from tkinter import messagebox

from appcommon import VERSION
from theming import FONT_UI
import find_games as fg
from find_games import (
    save_name_override, search_sgdb_autocomplete,
    load_prefs, save_prefs, DEFAULT_PREFS,
)


def show_info(app):
    """Display a scrollable quick-start guide window."""
    t = app.theme
    info = tk.Toplevel(app.window)
    info.title("Quick Start Guide")
    info.geometry("520x500")
    info.minsize(420, 360)
    info.resizable(True, True)
    info.config(bg=t["bg"])
    info.update_idletasks()
    x = app.window.winfo_x() + (app.window.winfo_width() // 2) - 260
    y = app.window.winfo_y() + (app.window.winfo_height() // 2) - 250
    info.geometry(f"+{x}+{y}")

    tk.Label(info, text="Welcome to NonSteamScraper",
              font=(FONT_UI, 16, "bold"), bg=t["bg"], fg=t["fg"]).pack(pady=(12, 0))
    tk.Label(info, text=f"v{VERSION}",
              font=(FONT_UI, 10), bg=t["bg"], fg=t["muted"]).pack()

    def close_info():
        app._close_modal(info)
        info.destroy()

    # Close button pinned at the bottom so it is always reachable
    app._btn(info, "Got it", close_info, primary=True, font=(FONT_UI, 11)).pack(side="bottom", pady=12)
    info.protocol("WM_DELETE_WINDOW", close_info)

    # Scrollable body
    canvas, body = app._make_scrollable_frame(info, padx=10)

    def section(title, lines):
        tk.Label(body, text=title, font=(FONT_UI, 11, "bold"),
                  bg=t["bg"], fg=t["fg"], anchor="w").pack(fill="x", pady=(8, 0))
        for ln in lines:
            tk.Label(body, text=ln, font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"],
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
                    font=(FONT_UI, 10, "underline"), fg=t["link"],
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

    app._bind_wheel_to_canvas(canvas, canvas)
    app._open_modal(info)


def open_sgdb_search(app, game):
    """Open a live SGDB search dialog; on selection, save override and re-fetch."""
    t = app.theme
    dlg = tk.Toplevel(app.window)
    dlg.title("Find on SteamGridDB")
    dlg.minsize(420, 300)
    dlg.config(bg=t["bg"])

    def _close_dlg(evt=None):
        app._close_modal(dlg)
        dlg.destroy()

    # Centre over parent window
    app.window.update_idletasks()
    px = app.window.winfo_x() + app.window.winfo_width() // 2 - 210
    py = app.window.winfo_y() + app.window.winfo_height() // 2 - 180
    dlg.geometry(f"420x360+{px}+{py}")

    tk.Label(dlg, text=game["name"][:48], font=(FONT_UI, 11, "bold"),
             bg=t["bg"], fg=t["fg"]).pack(pady=(12, 2), padx=12)

    search_var = tk.StringVar(value=game["name"])
    entry = tk.Entry(dlg, textvariable=search_var, font=(FONT_UI, 11),
                     bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"],
                     relief="flat", bd=4,
                     highlightthickness=1, highlightbackground=t["entry_bg"],
                     highlightcolor=t["accent"])
    entry.pack(fill="x", padx=12, pady=4)
    entry.select_range(0, "end")
    entry.focus_set()

    status_lbl = tk.Label(dlg, text="", font=(FONT_UI, 9),
                           bg=t["bg"], fg=t["muted"])
    status_lbl.pack(padx=12, anchor="w")

    list_frame = tk.Frame(dlg, bg=t["bg"])
    list_frame.pack(fill="both", expand=True, padx=12, pady=4)
    scrollbar = tk.Scrollbar(list_frame)
    scrollbar.pack(side="right", fill="y")
    listbox = tk.Listbox(list_frame, font=(FONT_UI, 10),
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
        app._remove_from_skip(game["app_id"])
        # fg.GRID_FOLDER (not the by-value import) so account switches are honored.
        for f in glob.glob(os.path.join(fg.GRID_FOLDER, f"{game['app_id']}*")):
            try:
                os.remove(f)
            except Exception:
                pass
        app.log(f"Matched '{game['name']}' → '{chosen['name']}' (SGDB #{chosen['id']})",
                 icon="search")
        _close_dlg()
        app.load_games()
        app.start_fetch()

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

    app._btn(btn_bar, "Select", _apply, primary=True).pack(side="right")
    app._btn(btn_bar, "Cancel", _close_dlg).pack(side="right", padx=(0, 6))

    # Kick off initial search with the game's current name
    _do_search(game["name"])
    app._open_modal(dlg)


def open_art_prefs(app):
    """Open the art style preferences window."""
    t = app.theme
    win = tk.Toplevel(app.window)
    win.title("Art Style Preferences")
    win.geometry("500x580")
    win.minsize(440, 420)
    win.resizable(True, True)
    win.config(bg=t["bg"])
    win.update_idletasks()
    x = app.window.winfo_x() + (app.window.winfo_width() // 2) - 250
    y = app.window.winfo_y() + (app.window.winfo_height() // 2) - 290
    win.geometry(f"+{x}+{y}")

    prefs = load_prefs()
    vars_ = {k: tk.StringVar(value=v) for k, v in prefs.items()}
    update_fns = {}

    def do_save():
        save_prefs({k: v.get() for k, v in vars_.items()})
        app.log("Art style preferences saved.", icon="palette")
        app._close_modal(win)
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
    reset_btn = app._btn(header_row, "Reset to defaults", do_reset, font=(FONT_UI, 9))
    app._iconify(reset_btn, "undo", app.ICON_ACTION, compound="left")
    reset_btn.pack(side="left")
    tk.Label(header_row, text="Art Style Preferences", font=(FONT_UI, 16, "bold"),
             bg=t["bg"], fg=t["fg"]).pack(side="left", expand=True)

    app._btn(win, "Save & Close", do_save, primary=True, font=(FONT_UI, 11)).pack(side="bottom", pady=12)
    win.protocol("WM_DELETE_WINDOW", do_save)

    canvas, body = app._make_scrollable_frame(win, padx=10)

    def pref_row(parent, label, key, tip=None):
        # 3-state style control: Prefer (must_have) / Allow (ok) / Exclude (never).
        # "Allow" is the neutral default; only Prefer/Exclude get a strong color.
        var = vars_[key]
        row = tk.Frame(parent, bg=t["bg"])
        row.pack(fill="x", pady=2, padx=5)
        lbl = tk.Label(row, text=label, font=(FONT_UI, 10), bg=t["bg"], fg=t["fg"],
                       width=18, anchor="w")
        lbl.pack(side="left")
        if tip:
            app._add_tooltip(lbl, tip)
        btns = {}
        # (value, button text, selected-bg, selected-fg)
        for val, txt, col, sel_fg in [("must_have", "Prefer",  t["ok"],        "#ffffff"),
                                      ("ok",        "Allow",   t["select_bg"], t["fg"]),
                                      ("never",     "Exclude", t["danger"],    "#ffffff")]:
            b = tk.Button(row, text=txt, font=(FONT_UI, 8), width=9,
                          bg=t["button_bg"], fg=t["button_fg"],
                          activebackground=t["select_bg"], relief="groove",
                          command=lambda v=val, k=key: set_val(k, v))
            b.pack(side="left", padx=2)
            btns[val] = (b, col, sel_fg)

        def update(bmap=btns, v=var):
            cur = v.get()
            for val, (b, col, sel_fg) in bmap.items():
                if val == cur:
                    b.config(bg=col, fg=sel_fg, relief="sunken")
                else:
                    b.config(bg=t["button_bg"], fg=t["button_fg"], relief="groove")

        update_fns[key] = update
        update()

    def section(title):
        tk.Label(body, text=title, font=(FONT_UI, 10, "bold"),
                 bg=t["bg"], fg=t["fg"], anchor="w").pack(fill="x", padx=5, pady=(10, 1))
        tk.Frame(body, bg=t["muted"], height=1).pack(fill="x", padx=5, pady=(0, 4))

    # Explainer for the Prefer / Allow / Exclude model.
    tk.Label(
        body,
        text=("Prefer = try these first (results broaden automatically if scarce).  "
              "Allow = include normally.  Exclude = never use.  "
              "You won't get zero results."),
        font=(FONT_UI, 9), bg=t["bg"], fg=t["muted"],
        justify="left", anchor="w", wraplength=440,
    ).pack(fill="x", padx=5, pady=(4, 2))

    section("Content")
    pref_row(body, "Animated art  ", "animated",
             tip=("Animated artwork. Pick one in the results screen to convert it to "
                  "APNG (the only animated format Steam shows)."))
    pref_row(body, "NSFW", "nsfw", tip="Explicit / adult artwork.")
    pref_row(body, "Humor / Memes", "humor", tip="Meme / joke artwork.")
    section("Cover (Grid)")
    pref_row(body, "No logo overlay", "grid_no_logo",
             tip="Cover art with no game logo/title text baked in.")
    pref_row(body, "Alternate style", "grid_alternate",
             tip="Non-standard / alternative cover designs.")
    pref_row(body, "Blurred", "grid_blurred",
             tip="Covers with a blurred background.")
    pref_row(body, "Material design", "grid_material",
             tip="Flat, 'material design'-style covers.")
    section("Hero / Background")
    pref_row(body, "Alternate art", "hero_alternate",
             tip="Alternative hero / banner designs.")
    pref_row(body, "Blurred", "hero_blurred",
             tip="Hero banners with a blurred background.")
    section("Logo")
    pref_row(body, "Official", "logo_official", tip="The official game logo.")
    pref_row(body, "White style", "logo_white", tip="A white version of the logo.")
    pref_row(body, "Black style", "logo_black", tip="A black version of the logo.")
    section("Icon")
    pref_row(body, "Official", "icon_official", tip="The official game icon.")

    app._bind_wheel_to_canvas(canvas, canvas)
    app._open_modal(win)
