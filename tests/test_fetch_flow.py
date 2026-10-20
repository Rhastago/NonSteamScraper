"""Behavioural tests for the fetch flow in the UI mixins, run headless.

fetch_mixin / library_mixin / geometry_mixin import tkinter, which needs a display
(and is not even installed in the dev venv). These tests load them under a stand-in
`tkinter` module (and stand-in widgets), then drive the real mixin methods on a fake
app object. Only the widget layer is faked: the fetch loop, the key check, the busy
state and the skip-list decisions all run for real.

The stand-in modules are installed into sys.modules for this file only and removed
again afterwards, so no other test can pick them up.

HOME is sandboxed by tests/conftest.py before the backend is imported (importing
appfiles binds every ~/.steamart_* path to HOME).
"""

import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

assert os.path.basename(os.environ["HOME"]).startswith("steamart_test_home_"), (
    "tests/conftest.py must sandbox HOME before the backend is imported")

import artwork
import restore
import steam_files
import sgdb
import steam_env
import appfiles  # noqa: E402


# ---------------------------------------------------------------------------
# Stand-in tkinter
# ---------------------------------------------------------------------------

class StandInWidget:
    """Every tkinter class (Toplevel, Button, Label, StringVar, OptionMenu, …).
    Records its constructor options and every config() in .opts, so a window
    function such as open_settings can be BUILT headless and its controls found
    and pressed. Any other Tk method is a no-op. Every instance lands in
    StandInWidget.created (reset per test by the fixtures that use it)."""
    created = []
    pack_order = []

    def __init__(self, *args, **opts):
        self.args = args
        self.opts = dict(opts)
        self.destroyed = False
        StandInWidget.created.append(self)

    def config(self, **kw):
        self.opts.update(kw)
    configure = config

    def pack(self, *a, **kw):
        # Records the pack options and the order of every pack() call.
        StandInWidget.pack_order.append(self)
        self.packed = dict(kw)

    def cget(self, key):
        return self.opts.get(key)

    def get(self):
        return self.opts.get("value")

    def set(self, value):
        self.opts["value"] = value

    def destroy(self):
        self.destroyed = True

    def winfo_exists(self):
        return not self.destroyed

    def winfo_children(self):
        return []

    def winfo_reqheight(self):
        return 100

    def winfo_reqwidth(self):
        return 100

    def winfo_rootx(self):
        return 0

    def winfo_rooty(self):
        return 0

    def __setitem__(self, key, value):
        self.opts[key] = value

    def __getitem__(self, key):
        # w["menu"] (an OptionMenu's drop-down) — a stand-in of its own.
        return self.opts.setdefault(("item", key), StandInWidget())

    def after(self, ms, fn=None, *args):
        # like FakeWindow: the Tk thread is the test thread; after(0) runs at once,
        # a delayed call is recorded in .delayed (pump() runs them).
        if ms == 0 and fn is not None:
            fn(*args)
        elif fn is not None:
            self.opts.setdefault("_delayed", []).append((fn, args))
        return "after#"

    def pump(self, rounds=50):
        """Run the recorded delayed calls (a poll that reschedules is run again),
        up to `rounds` passes."""
        for _ in range(rounds):
            pending = self.opts.pop("_delayed", [])
            if not pending:
                return
            for fn, args in pending:
                fn(*args)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


_Dummy = StandInWidget


class StandInListbox(StandInWidget):
    """A Listbox whose selection a test can set."""
    def curselection(self):
        return tuple(self.opts.get("selection", ()))

    def selection_set(self, index):
        self.opts["selection"] = (index,)


def _fake_tk_modules():
    """Build the stand-in tkinter, tkinter.messagebox and tkinter.ttk modules.
    messagebox records every call in .calls and answers askyesno with .answer."""
    tk = types.ModuleType("tkinter")
    kinds = {}

    def widget_class(name):
        # One StandInWidget subclass per tkinter name, so a test can tell an
        # OptionMenu from a Button by type(w).__name__.
        if name.startswith("__"):
            raise AttributeError(name)
        if name not in kinds:
            base = StandInListbox if name == "Listbox" else StandInWidget
            kinds[name] = type(name, (base,), {})
        return kinds[name]
    tk.__getattr__ = widget_class
    tk.TclError = type("TclError", (Exception,), {})
    mb = types.ModuleType("tkinter.messagebox")
    mb.calls = []
    mb.answer = True
    mb.while_open = None   # run while a dialog is "open" (the Tk queue keeps going)

    def _record(kind):
        def f(*a, **k):
            mb.calls.append((kind, a, k))
            if mb.while_open:
                mb.while_open()
            return mb.answer if kind == "askyesno" else "ok"
        return f
    for kind in ("showinfo", "showwarning", "showerror", "askyesno"):
        setattr(mb, kind, _record(kind))
    ttk = types.ModuleType("tkinter.ttk")
    ttk.__getattr__ = lambda name: _Dummy
    tk.messagebox = mb
    tk.ttk = ttk
    return {"tkinter": tk, "tkinter.messagebox": mb, "tkinter.ttk": ttk}


_UI_MODULES = ("fetch_mixin", "library_mixin", "geometry_mixin", "ui_mixin",
               "settings_window", "results_window", "dialogs", "imaging",
               "PIL.ImageTk", "app")


@pytest.fixture(scope="module")
def ui():
    """Import the mixins under the stand-in tkinter. Yields a namespace with the
    modules and the fake messagebox; restores sys.modules afterwards."""
    saved = {name: sys.modules.get(name)
             for name in list(_fake_tk_modules()) + list(_UI_MODULES)}
    fakes = _fake_tk_modules()
    for name in _UI_MODULES:
        sys.modules.pop(name, None)
    sys.modules.update(fakes)
    try:
        import fetch_mixin
        import library_mixin
        import geometry_mixin
        import ui_mixin
        import settings_window
        import results_window
        import imaging
        import dialogs
        yield types.SimpleNamespace(
            fetch_mixin=fetch_mixin, library_mixin=library_mixin,
            geometry_mixin=geometry_mixin, ui_mixin=ui_mixin,
            settings_window=settings_window, results_window=results_window,
            imaging=imaging, dialogs=dialogs,
            mb=fakes["tkinter.messagebox"])
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


class FakeWidget:
    """Records the last value of every config() option; pack/forget are no-ops."""
    def __init__(self, **opts):
        self.opts = dict(opts)

    def config(self, **kw):
        self.opts.update(kw)
    configure = config

    def cget(self, key):
        return self.opts.get(key)

    def set(self, value):
        self.opts["value"] = value

    def pack(self, *a, **k):
        pass

    def pack_forget(self):
        pass

    def winfo_exists(self):
        return True


class FakeWindow(FakeWidget):
    """after(0, fn) runs fn at once (the Tk thread is the test thread); a delayed
    after() is recorded, not run. destroy() is recorded."""
    def __init__(self):
        super().__init__()
        self.delayed = []
        self.destroyed = False

    def after(self, ms, fn=None, *args):
        if ms == 0 and fn is not None:
            fn(*args)
        else:
            self.delayed.append((ms, fn))
        return "after#"

    def destroy(self):
        self.destroyed = True

    def lift(self):
        pass

    def focus_force(self):
        pass

    def update_idletasks(self):
        pass

    def winfo_x(self):
        return 0
    winfo_y = winfo_width = winfo_height = winfo_x


def make_app(ui, games=()):
    """A fake SteamArtApp built from the real mixins, with stand-in widgets."""
    class FakeApp(ui.library_mixin.LibraryMixin, ui.fetch_mixin.FetchMixin,
                  ui.geometry_mixin.GeometryMixin, ui.ui_mixin.UIMixin):
        def __init__(self):
            self.window = FakeWindow()
            self.games = [dict(g) for g in games]
            self.fetch_button = FakeWidget(state="normal")
            self.undo_button = FakeWidget(state="normal")
            self.summary_label = FakeWidget()
            self.progress_var = FakeWidget()
            self.progress_label = FakeWidget()
            self.progress_bar = FakeWidget()
            self.log_toggle = FakeWidget()
            self.status = []
            self.logged = []
            self.settings_opened = 0
            self.results_shown = []
            self.loads = 0
            self._fetched_this_session = False
            self._restore_point_inherited = False
            self._fetch_busy = False
            self._fetch_cancel = None
            self._fetch_followup = None
            self._fetch_run = None
            self._closing = False
            self._apply_targets = []
            self._settings_on_fetch_end = None
            self._results_view = None
            self._thumb_executor = types.SimpleNamespace(shutdown=lambda wait=True: None)
            self.theme = {"warn": "#d17a00", "fg": "#000000"}
            self.theme_name = "dark"
            self.accent_name = "blue"

        # widget-layer stand-ins
        def log(self, message, icon=None):
            self.logged.append(message)

        def _set_status(self, text, fg=None, icon=None):
            self.status.append(text)

        def _refit_window_height(self):
            pass

        def _render_list(self):
            pass

        def open_settings(self):
            self.settings_opened += 1
            self.settings_opened_while_busy = self._fetch_busy

        def show_results(self, fetch_results):
            self.results_shown.append(fetch_results)

        def check_steam_running(self):
            pass

    return FakeApp()


def make_settings_app(ui, monkeypatch, busy=False, users=("111",)):
    """A fake app that can BUILD the real settings_window.open_settings under the
    stand-in tkinter: real UIMixin widget helpers and modal stack, a full theme,
    no icons, no work-area probing. Backend reads are patched in settings_window."""
    from theming import resolve_theme
    sw = ui.settings_window
    monkeypatch.setattr(sw, "get_all_steam_users", lambda: list(users))
    monkeypatch.setattr(sw, "is_steam_running", lambda *a, **k: False)
    monkeypatch.setattr(sw, "get_cache_size", lambda: 1.5)
    monkeypatch.setattr(sw, "load_api_key", lambda: "KEY")
    monkeypatch.setattr(steam_env, "get_steam_user_personas", lambda: {})
    monkeypatch.setattr(steam_env, "get_active_user", lambda: users[0])
    monkeypatch.setattr(restore, "restore_point_size", lambda: 0.0)
    monkeypatch.setattr(steam_files, "legacy_backup_size", lambda: 0.0)
    app = make_app(ui)
    app.theme = resolve_theme("dark", "blue")
    app._icon_cache = {}
    app._modal_stack = []
    app.ICON_BTN = app.ICON_INLINE = app.ICON_ACTION = app.ICON_NAV = 24
    app._icon = lambda name, size=18: None
    app._fit_window_to_workarea = lambda win, desired_w, desired_h: (desired_w, desired_h)
    app._place_over_main = lambda win, desired_w, desired_h: (desired_w, desired_h)
    app._fetch_busy = busy
    return app


def open_settings_headless(ui, app):
    """Build Settings for `app`; return the widgets it created."""
    StandInWidget.created = []
    ui.settings_window.open_settings(app)
    return list(StandInWidget.created)


def widget_by_text(widgets, text):
    hits = [w for w in widgets if w.opts.get("text") == text]
    assert len(hits) == 1, f"expected one widget reading {text!r}, found {len(hits)}"
    return hits[0]


def _fetched_art(slots):
    art = artwork.FetchedArt()
    art.slots.update(slots)
    return art


def _game(app_id, name=None, has_art=False):
    return {"name": name or f"Game {app_id}", "app_id": app_id,
            "has_art": has_art, "skipped": False}


@pytest.fixture
def fetch_env(ui, tmp_path, monkeypatch):
    """Patch everything the fetch body reaches outside itself. Returns a recorder:
    searched names, downloaded ids, skip-list writes and restore-point calls.
    `outcomes` maps app_id -> what the patched search_game returns."""
    fm = ui.fetch_mixin
    rec = types.SimpleNamespace(searched=[], downloaded=[], skipped=[], calls=[],
                                outcomes={}, library=[], on_search=None)

    def fake_search(name, app_id=None):
        rec.searched.append(app_id)
        if rec.on_search:
            rec.on_search(app_id)
        return rec.outcomes.get(app_id, (sgdb.SEARCH_FOUND, 4242))

    def fake_download(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        rec.downloaded.append(app_id)
        return artwork.FetchedArt()

    # operations.run_fetch reads the backend modules live, so these are patched there.
    monkeypatch.setattr(sgdb, "search_game", fake_search)
    monkeypatch.setattr(artwork, "download_all_artwork", fake_download)
    monkeypatch.setattr(appfiles, "add_to_skip_list", lambda app_id: rec.skipped.append(app_id))
    monkeypatch.setattr(fm, "load_prefs", lambda: {})
    monkeypatch.setattr(fm, "load_api_key", lambda: "KEY")
    monkeypatch.setattr(sgdb, "check_api_key", lambda key=None: sgdb.KEY_ACCEPTED)
    monkeypatch.setattr(restore, "begin_restore_run", lambda: rec.calls.append("begin"))
    monkeypatch.setattr(restore, "seal_restore_point", lambda: rec.calls.append("seal"))
    monkeypatch.setattr(steam_env, "is_steam_running", lambda *a, **k: False)
    monkeypatch.setattr(steam_files, "set_shortcut_icons", lambda m: len(m))
    monkeypatch.setattr(steam_files, "save_pending_icons", lambda m: None)
    monkeypatch.setattr(restore, "has_restore_point", lambda: True)
    monkeypatch.setattr(ui.library_mixin, "get_non_steam_games",
                        lambda: [dict(g) for g in rec.library])
    ui.mb.calls.clear()
    ui.mb.answer = True
    ui.mb.while_open = None
    return rec


# ---------------------------------------------------------------------------
# T1 / Q1 — Search → Select fetches only the matched game
# ---------------------------------------------------------------------------

def test_fetch_body_with_games_fetches_only_those_games(ui, fetch_env):
    """Q1: start_fetch(games=[game]) — the run covers the matched game, not every
    game in the library that is missing art."""
    a, b, c = _game(1), _game(2), _game(3)
    app = make_app(ui, [a, b, c])

    app._run_fetch_body([b])

    assert fetch_env.searched == [2]
    assert fetch_env.downloaded == [2]


def test_fetch_body_without_games_fetches_every_game_missing_art(ui, fetch_env):
    app = make_app(ui, [_game(1), _game(2, has_art=True), _game(3)])

    app._run_fetch_body()

    assert fetch_env.searched == [1, 3]


# ---------------------------------------------------------------------------
# T2 / Q2 — the key is checked before the run; a refusal never hides a game
# ---------------------------------------------------------------------------

def test_rejected_key_runs_nothing_and_opens_settings(ui, fetch_env, monkeypatch):
    monkeypatch.setattr(sgdb, "check_api_key", lambda key=None: sgdb.KEY_REJECTED)
    fetch_env.library = [_game(1), _game(2)]
    app = make_app(ui, fetch_env.library)
    app._fetch_busy = True

    app.run_fetch()

    assert "begin" not in fetch_env.calls, "begin_restore_run must not run"
    assert fetch_env.searched == [] and fetch_env.skipped == []
    popups = [c for c in ui.mb.calls if c[0] == "showwarning"]
    assert len(popups) == 1
    _kind, args, _kw = popups[0]
    assert args[0] == "API Key Rejected"
    assert args[1] == ("SteamGridDB rejected your API key, so nothing was fetched."
                       "\n\nOpen Settings to check or replace it.")
    assert app.settings_opened == 1
    assert app.settings_opened_while_busy is False, (
        "Settings must open after the busy state cleared, or its controls are disabled")
    assert app._fetch_busy is False, "an early return must still end the busy state"
    assert app.fetch_button.opts["state"] == "normal"


def test_unreachable_key_check_runs_nothing_and_blames_no_key(ui, fetch_env, monkeypatch):
    monkeypatch.setattr(sgdb, "check_api_key", lambda key=None: sgdb.KEY_UNREACHABLE)
    app = make_app(ui, [_game(1)])

    app._run_fetch_body()

    assert "begin" not in fetch_env.calls
    assert fetch_env.searched == [] and fetch_env.skipped == []
    assert ui.mb.calls == [], "no popup when SteamGridDB cannot be reached"
    assert app.settings_opened == 0
    assert app.status[-1] == ("Couldn't reach SteamGridDB — nothing was fetched. "
                              "Check your connection and try again.")


def test_key_check_runs_before_begin_restore_run(ui, fetch_env, monkeypatch):
    monkeypatch.setattr(sgdb, "check_api_key",
                        lambda key=None: fetch_env.calls.append("check") or sgdb.KEY_ACCEPTED)
    app = make_app(ui, [_game(1)])

    app._run_fetch_body()

    assert fetch_env.calls[:2] == ["check", "begin"]


def test_refused_search_is_not_skip_listed_and_stops_the_run(ui, fetch_env):
    """Safety net (Q2): a refusal mid-run stops the loop at that game, hides
    nothing, and still seals the games already done."""
    fetch_env.outcomes = {2: (sgdb.SEARCH_REFUSED, None)}
    app = make_app(ui, [_game(1), _game(2), _game(3)])

    app._run_fetch_body()

    assert fetch_env.searched == [1, 2], "game 3 must never be searched"
    assert fetch_env.downloaded == [1]
    assert fetch_env.skipped == []
    assert ("SteamGridDB rejected the API key — fetch stopped. No games were hidden."
            in app.logged)
    assert "seal" in fetch_env.calls
    assert app.status[-1] == "Fetch stopped — SteamGridDB rejected the API key."


def test_not_found_is_skip_listed_but_a_network_error_is_not(ui, fetch_env):
    fetch_env.outcomes = {1: (sgdb.SEARCH_NOT_FOUND, None), 2: (sgdb.SEARCH_ERROR, None)}
    app = make_app(ui, [_game(1), _game(2), _game(3)])

    app._run_fetch_body()

    assert fetch_env.skipped == [1]
    assert fetch_env.searched == [1, 2, 3]
    assert fetch_env.downloaded == [3]


# ---------------------------------------------------------------------------
# T3 / B5 — the fetch log never says "saved" for a slot that failed
# ---------------------------------------------------------------------------

def _slot(label, failed, applied=True):
    return {"label": label, "failed": failed,
            "applied_index": 0 if applied and not failed else None,
            "applied_path": f"/g/{label}.png"}


@pytest.mark.parametrize("slots,expect_start,expect_in", [
    ({"grids": _slot("Cover", False)}, "Artwork saved for G", None),
    ({"grids": _slot("Cover", True), "heroes": _slot("Hero/Background", False)},
     "Artwork saved for G", "download failed for Cover; the previous art was kept"),
    ({"grids": _slot("Cover", True), "icons": _slot("Icon", True)},
     "Download failed for G", "Cover, Icon; the previous art was kept"),
])
def test_fetch_log_reports_failed_slots(ui, fetch_env, monkeypatch, slots, expect_start, expect_in):
    def fake_download(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        return _fetched_art(slots)
    monkeypatch.setattr(artwork, "download_all_artwork", fake_download)
    app = make_app(ui, [_game(1, name="G")])

    app._run_fetch_body()

    line = next(m for m in app.logged if m.startswith(("Artwork saved", "Download failed")))
    assert line.startswith(expect_start)
    if expect_in:
        assert expect_in in line


# ---------------------------------------------------------------------------
# T-A1 / Q3, Q4 — one busy state while a fetch runs
# ---------------------------------------------------------------------------

def test_load_games_while_busy_keeps_fetch_and_undo_disabled(ui, fetch_env):
    """B2: Refresh / Ignore / Restore / Re-fetch call load_games, which turned
    Fetch and Undo back on in the middle of a run."""
    fetch_env.library = [_game(1), _game(2)]   # both need art; a restore point exists
    app = make_app(ui)

    app._fetch_busy = True
    app.load_games()
    assert app.fetch_button.opts["state"] == "disabled"
    assert app.undo_button.opts["state"] == "disabled"

    app._fetch_busy = False
    app.load_games()
    assert app.fetch_button.opts["state"] == "normal"
    assert app.undo_button.opts["state"] == "normal"


class _ThreadRecorder:
    started = []

    def __init__(self, target=None, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        _ThreadRecorder.started.append(self)


@pytest.fixture
def no_threads(ui, monkeypatch):
    """start_fetch's worker is recorded, not started; the test runs it itself."""
    import threading
    _ThreadRecorder.started = []
    monkeypatch.setattr(ui.fetch_mixin, "threading", types.SimpleNamespace(
        Thread=_ThreadRecorder, Event=threading.Event))
    return _ThreadRecorder.started


def test_second_start_fetch_while_busy_does_nothing(ui, fetch_env, no_threads):
    app = make_app(ui, [_game(1)])

    app.start_fetch()
    assert len(no_threads) == 1 and app._fetch_busy is True

    app.fetch_button.opts["text"] = "untouched"
    app.start_fetch()
    app.start_fetch(games=[_game(1)])
    assert len(no_threads) == 1, "no second worker may start while one runs"
    assert app.fetch_button.opts["text"] == "untouched"


def test_busy_state_clears_after_a_worker_exception(ui, fetch_env, no_threads, monkeypatch):
    fetch_env.library = [_game(1)]
    app = make_app(ui, fetch_env.library)
    app.start_fetch()
    assert app._fetch_busy is True

    def boom(games=None):
        raise RuntimeError("worker blew up")
    monkeypatch.setattr(app, "_run_fetch_body", boom)
    no_threads[0].target(*no_threads[0].args)

    assert app._fetch_busy is False
    assert app.fetch_button.opts["state"] == "normal"
    assert "Fetch failed: worker blew up" in app.logged


def test_busy_state_clears_at_the_end_of_a_run_and_results_open(ui, fetch_env, no_threads):
    fetch_env.library = [_game(1)]
    app = make_app(ui, fetch_env.library)
    app.start_fetch()

    no_threads[0].target(*no_threads[0].args)

    assert app._fetch_busy is False
    assert len(app.results_shown) == 1 and app.results_shown[0][0]["app_id"] == 1


def test_write_commands_refuse_while_busy(ui, fetch_env, monkeypatch):
    """Q3: Re-fetch, Ignore, Restore, Undo, theme and accent refuse with a
    status line while a fetch runs — and do their job when it does not."""
    done = []
    monkeypatch.setattr(restore, "clear_game_art", lambda uid: done.append(("clear", uid)))
    monkeypatch.setattr(appfiles, "add_to_skip_list", lambda uid: done.append(("skip", uid)))
    monkeypatch.setattr(restore, "undo_restore_point", lambda: done.append("undo") or
                        {"games": 0, "files": 0, "removed": 0, "queued": False,
                         "failed": 0, "icon_write_failed": False})
    monkeypatch.setattr(ui.ui_mixin, "save_theme", lambda n: done.append(("theme", n)))
    monkeypatch.setattr(ui.ui_mixin, "save_accent", lambda n: done.append(("accent", n)))
    monkeypatch.setattr(ui.ui_mixin, "ACCENTS", {"blue": {}, "green": {}})
    app = make_app(ui)
    monkeypatch.setattr(appfiles, "remove_from_skip_list", lambda uid: done.append(("unskip", uid)))
    monkeypatch.setattr(app, "relaunch_app", lambda: done.append("relaunch"))
    g = _game(5)

    def run_all():
        app.refetch_game(g)
        app.ignore_game(g)
        app.restore_game(g)
        app.undo_fetch()
        app.toggle_theme()
        app.set_accent("green")

    app._fetch_busy = True
    run_all()
    assert done == [], f"nothing may write while a fetch runs, got {done}"
    assert app.status[-1] == ui.fetch_mixin.FetchMixin.BUSY_MESSAGE

    app._fetch_busy = False
    run_all()
    for expected in [("clear", 5), ("skip", 5), ("unskip", 5), "undo",
                     ("theme", "light"), ("accent", "green"), "relaunch"]:
        assert expected in done


def test_close_while_idle_closes_at_once(ui, fetch_env):
    app = make_app(ui)
    app._on_close()
    assert app.window.destroyed and ui.mb.calls == []


def test_close_during_fetch_asks_and_no_keeps_running(ui, fetch_env, no_threads):
    app = make_app(ui, [_game(1)])
    app.start_fetch()
    ui.mb.answer = False

    app._on_close()

    assert [c[0] for c in ui.mb.calls] == ["askyesno"]
    assert ui.mb.calls[0][1] == ("Fetch in progress", "A fetch is running — stop and close?")
    assert not app.window.destroyed
    assert not app._fetch_cancel.is_set() and app._closing is False


def test_close_during_fetch_stops_between_games_and_removes_its_parts(
        ui, fetch_env, no_threads, tmp_path, monkeypatch):
    """Q4: yes → the run stops after the game it is on, the art of finished
    games is sealed, the run's .part files go, and only then does the window close."""
    grid, cache = tmp_path / "grid", tmp_path / "cache"
    grid.mkdir(); cache.mkdir()
    for f in ("1p.png", "1p.png.part", "2_hero.png.part", "10p.png.part"):
        (grid / f).write_bytes(b"x")
    (cache / "1p_0.png.part").write_bytes(b"x")
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(grid))
    monkeypatch.setattr(appfiles, "CACHE_FOLDER", str(cache))
    fetch_env.library = [_game(1), _game(2), _game(3)]
    app = make_app(ui, fetch_env.library)
    app.start_fetch()
    # The user closes the window while game 1 is being searched.
    fetch_env.on_search = lambda app_id: app._on_close() if app_id == 1 else None

    no_threads[0].target(*no_threads[0].args)

    assert fetch_env.searched == [1], "the run must stop before game 2"
    assert fetch_env.downloaded == [1], "the game in progress finishes"
    assert "seal" in fetch_env.calls
    assert app.window.destroyed
    assert app.results_shown == [], "no results screen for a window that is closing"
    assert sorted(os.listdir(grid)) == ["10p.png.part", "1p.png"], (
        "the run's .part files go; game 10 was not in the run; real art stays")
    assert os.listdir(cache) == []


def test_close_yes_after_the_fetch_ended_under_the_question_closes_at_once(
        ui, fetch_env, no_threads):
    """C1: the fetch ends while "stop and close?" is open (_end_fetch runs from the
    Tk queue under the modal dialog, not closing). Yes then closes the window at
    once — not after the CLOSE_FALLBACK_MS fallback."""
    fetch_env.library = [_game(1)]
    app = make_app(ui, fetch_env.library)
    app.start_fetch()
    assert app._fetch_busy is True
    ui.mb.while_open = lambda: app._end_fetch(None)
    ui.mb.answer = True

    app._on_close()

    assert app._fetch_busy is False
    assert app.window.destroyed, "Yes must close at once when the fetch already ended"
    assert not any(ms == app.CLOSE_FALLBACK_MS for ms, _fn in app.window.delayed), (
        "no fallback timer for a run that is already over")


# ---------------------------------------------------------------------------
# C2 / S1 — Settings → Clear Cache joins the busy list
# ---------------------------------------------------------------------------

def test_settings_clear_cache_is_greyed_and_refuses_during_a_fetch(ui, fetch_env, monkeypatch):
    cleared = []
    monkeypatch.setattr(ui.settings_window, "clear_cache", lambda: cleared.append(1))
    app = make_settings_app(ui, monkeypatch, busy=True)

    btn = widget_by_text(open_settings_headless(ui, app), "Clear Cache")

    assert btn.opts.get("state") == "disabled", "Clear Cache must be greyed out during a fetch"
    btn.opts["command"]()   # invoked anyway (a keyboard path, a race)
    assert cleared == [], "Clear Cache must refuse while a fetch runs"
    assert app.status[-1] == ui.fetch_mixin.FetchMixin.BUSY_MESSAGE


def test_settings_clear_cache_works_when_idle(ui, fetch_env, monkeypatch):
    cleared = []
    monkeypatch.setattr(ui.settings_window, "clear_cache", lambda: cleared.append(1))
    app = make_settings_app(ui, monkeypatch, busy=False)

    btn = widget_by_text(open_settings_headless(ui, app), "Clear Cache")

    assert btn.opts.get("state") in (None, "normal")
    btn.opts["command"]()
    assert cleared == [1]


# ---------------------------------------------------------------------------
# C3 / S2 — Settings left open across the end of a fetch comes back live
# ---------------------------------------------------------------------------

_WRITE_CONTROL_TEXTS = ("Switch to Light Mode", "Clear Cache", "Clear saved undo data",
                        "Clear All Artwork", "Reset App to Factory Defaults")
_ALWAYS_LIVE_TEXTS = ("Save", "Check for updates", "Restart Steam", "Close")


def _write_controls(widgets):
    """The controls Q3/S1 grey out: the five buttons above, the accent swatches
    (Buttons with an accent-colour bg and no command text) and the account menu."""
    named = [widget_by_text(widgets, t) for t in _WRITE_CONTROL_TEXTS]
    swatches = [w for w in widgets if type(w).__name__ == "Button"
                and w.opts.get("text") in ("", "✓") and w.opts.get("width") == 2]
    menus = [w for w in widgets if type(w).__name__ == "OptionMenu"]
    assert len(swatches) == 4 and len(menus) == 1
    return named + swatches + menus


def _busy_note(widgets):
    return [w for w in widgets
            if str(w.opts.get("text", "")).startswith("Fetch in progress —")]


def test_settings_opened_during_a_fetch_comes_back_live_when_it_ends(ui, fetch_env, monkeypatch):
    app = make_settings_app(ui, monkeypatch, busy=True, users=("111", "222"))
    widgets = open_settings_headless(ui, app)
    controls = _write_controls(widgets)
    note = _busy_note(widgets)
    assert len(note) == 1 and not note[0].destroyed
    assert all(w.opts.get("state") == "disabled" for w in controls)
    for text in _ALWAYS_LIVE_TEXTS:
        assert widget_by_text(widgets, text).opts.get("state") != "disabled", text

    app._end_fetch(None)   # the worker's last step, on the Tk thread

    assert all(w.opts.get("state") == "normal" for w in controls), (
        "every write control must come back live when the fetch ends")
    assert note[0].destroyed, "the 'Fetch in progress' note must go"
    assert app._settings_on_fetch_end is None, "the release runs once"


def test_settings_opened_while_idle_is_unaffected_by_a_fetch_end(ui, fetch_env, monkeypatch):
    app = make_settings_app(ui, monkeypatch, busy=False, users=("111", "222"))
    widgets = open_settings_headless(ui, app)

    assert _busy_note(widgets) == []
    assert app._settings_on_fetch_end is None, "an idle Settings registers nothing"
    assert all(w.opts.get("state") == "normal" for w in _write_controls(widgets))


def test_closing_settings_before_the_fetch_ends_drops_the_release(ui, fetch_env, monkeypatch):
    app = make_settings_app(ui, monkeypatch, busy=True)
    widgets = open_settings_headless(ui, app)
    assert app._settings_on_fetch_end is not None

    widget_by_text(widgets, "Close").opts["command"]()

    assert app._settings_on_fetch_end is None, (
        "a closed Settings window must not be called back when the fetch ends")


# ---------------------------------------------------------------------------
# Results window, built headless — "Apply this one" (B6/B7, C4)
# ---------------------------------------------------------------------------

class _ManualPool:
    """Stand-in for imaging's daemon pools: submit() returns a PENDING Future and
    records the job; a test finishes jobs in the order it chooses."""
    def __init__(self):
        from concurrent.futures import Future
        self._Future = Future
        self.jobs = []

    def submit(self, fn, *args):
        f = self._Future()
        self.jobs.append((f, fn, args))
        return f

    def finish(self, i):
        f, fn, args = self.jobs[i]
        try:
            f.set_result(fn(*args))
        except Exception as e:
            f.set_exception(e)

    def finish_all(self):
        for i in range(len(self.jobs)):
            if not self.jobs[i][0].done():
                self.finish(i)


class _InlineThreads:
    """Stand-in for a module's `threading`: Thread(...).start() is RECORDED, so a
    test can check nothing ran on the Tk thread, then run the worker itself."""
    def __init__(self):
        self.started = []
        outer = self

        class Thread:
            def __init__(self, target=None, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                outer.started.append(self)
        self.Thread = Thread

    def run_all(self):
        while self.started:
            t = self.started.pop(0)
            t.target(*t.args)


@pytest.fixture
def results_env(ui, fetch_env, monkeypatch):
    """A fake app that can build results_window sections; worker threads are
    recorded (run with .threads.run_all()); image decodes are skipped."""
    rw = ui.results_window
    threads = _InlineThreads()
    monkeypatch.setattr(rw, "threading", threads)
    monkeypatch.setattr(ui.imaging, "_DECODE_POOL", _ManualPool())   # decodes never run
    monkeypatch.setattr(ui.imaging, "_display_image_on_label", lambda *a, **k: None)
    rec = types.SimpleNamespace(installed=[], written=[], queued=[], threads=threads,
                                install_ok=True, steam_running=False)

    def fake_install(save_path, url, existing_files=None, animated=False,
                     progress_cb=None, status_cb=None):
        rec.installed.append((save_path, url, animated))
        return rec.install_ok
    monkeypatch.setattr(artwork, "install_into_slot", fake_install)
    monkeypatch.setattr(steam_env, "is_steam_running", lambda *a, **k: rec.steam_running)
    monkeypatch.setattr(steam_files, "set_shortcut_icon",
                        lambda uid, path: rec.written.append((uid, path)) or True)
    monkeypatch.setattr(steam_files, "save_pending_icons", lambda m: rec.queued.append(dict(m)))
    monkeypatch.setattr(steam_files, "load_pending_icons", lambda: {})
    monkeypatch.setattr(steam_files, "has_pending_icons", lambda: False)
    app = make_settings_app(ui, monkeypatch)
    rec.app = app
    rec.view = rw.ResultsWindow(app)
    rec.view.top = StandInWidget()
    return rec


def _slot_data(base, urls, animated, applied_index):
    ext = "png"
    return {"applied_path": f"/grid/{base}.{ext}", "applied_index": applied_index,
            "failed": False, "label": "Icon",
            "option_urls": list(urls), "preview_urls": list(urls),
            "option_meta": [{"animated": a, "nsfw": False, "humor": False, "steam": False}
                            for a in animated],
            "thumb_paths": [f"/cache/{base}_{i}.png" for i in range(min(3, len(urls)))],
            "filename_base": base, "cache_base": f"/cache/{base}"}


def build_results_section(env, app_id, slots):
    StandInWidget.created = []
    env.view.build_game_result_section(
        StandInWidget(), {"name": "G", "app_id": app_id, "results": _fetched_art(slots)})
    return list(StandInWidget.created)


def _apply_button(widgets):
    btns = [w for w in widgets if type(w).__name__ == "Button"
            and w.opts.get("text") in ("Apply this one", "Applied!", "Queued (close Steam)")]
    assert len(btns) == 1
    return btns[0]


@pytest.mark.parametrize("steam_running", [False, True])
def test_results_animated_icon_apply_writes_or_queues_the_icon(ui, results_env, steam_running):
    """C4: an animated pick in the ICON slot was placed in the grid but never
    written to shortcuts.vdf. Steam closed: written; Steam running: queued."""
    app = results_env.app
    results_env.steam_running = steam_running
    data = _slot_data("7_icon", ["https://x/anim.png"], [True], applied_index=None)
    btn = _apply_button(build_results_section(results_env, 7, {"icons": data}))

    btn.opts["command"]()
    assert results_env.installed == [], "the encode must not run on the Tk thread"
    results_env.threads.run_all()

    assert results_env.installed == [("/grid/7_icon.png", "https://x/anim.png", True)]
    if steam_running:
        assert results_env.written == []
        assert results_env.queued == [{7: "/grid/7_icon.png"}]
        assert btn.opts["text"] == "Queued (close Steam)"
    else:
        assert results_env.written == [(7, "/grid/7_icon.png")], (
            "an animated icon apply with Steam closed must write shortcuts.vdf")
        assert results_env.queued == []
        assert btn.opts["text"] == "Applied!"


def test_results_animated_cover_apply_writes_no_icon(ui, results_env):
    app = results_env.app
    data = _slot_data("7p", ["https://x/anim.webp"], [True], applied_index=None)
    btn = _apply_button(build_results_section(results_env, 7, {"grids": data}))
    btn.opts["command"]()
    results_env.threads.run_all()
    assert results_env.installed == [("/grid/7p.png", "https://x/anim.webp", True)]
    assert results_env.written == [] and results_env.queued == []


@pytest.mark.parametrize("ok", [True, False])
def test_results_static_apply_installs_on_a_worker_then_shows_the_outcome(ui, results_env, ok):
    """B6, behavioural (replaces the AST check that the worker called
    install_into_slot): the button command only locks the button and starts a
    worker; the install happens in the worker; the outcome is drawn after."""
    app = results_env.app
    results_env.install_ok = ok
    data = _slot_data("7p", ["https://x/a.png", "https://x/b.jpg"], [False, False],
                      applied_index=0)
    widgets = build_results_section(results_env, 7, {"grids": data})
    btn = _apply_button(widgets)
    next_btn = [w for w in widgets if w.opts.get("text") == "▶"][0]
    next_btn.opts["command"]()           # view option 2
    assert btn.opts["text"] == "Apply this one"

    btn.opts["command"]()
    assert btn.opts["state"] == "disabled" and btn.opts["text"] == "Applying…"
    assert results_env.installed == [], "the download must not run on the Tk thread"
    results_env.threads.run_all()

    assert results_env.installed == [("/grid/7p.jpg", "https://x/b.jpg", False)]
    assert btn.opts["state"] == "normal"
    assert btn.opts["text"] == ("Applied!" if ok else "Failed — retry")
    assert results_env.written == [], "a cover apply never writes shortcuts.vdf"


# ---------------------------------------------------------------------------
# Search → Select, built headless (B1, Q1) — replaces the AST checks on _apply
# ---------------------------------------------------------------------------

@pytest.fixture
def search_env(ui, fetch_env, monkeypatch):
    dl = ui.dialogs
    threads = _InlineThreads()
    monkeypatch.setattr(dl, "threading", threads)
    rec = types.SimpleNamespace(calls=[], threads=threads, fetches=[])
    rec.answer = (sgdb.SEARCH_FOUND, [
        {"id": 2016, "name": "DOOM", "year": "2016", "type": "game"},
        {"id": 1993, "name": "DOOM", "year": "1993", "type": "game"}])
    monkeypatch.setattr(dl, "search_sgdb_autocomplete", lambda q: rec.answer)
    monkeypatch.setattr(appfiles, "save_id_override",
                        lambda uid, sid: rec.calls.append(("id", uid, sid)))
    monkeypatch.setattr(appfiles, "save_name_override",
                        lambda uid, name: rec.calls.append(("name", uid, name)))
    monkeypatch.setattr(appfiles, "remove_from_skip_list", lambda uid: rec.calls.append(("unskip", uid)))
    monkeypatch.setattr(restore, "clear_game_art", lambda uid: rec.calls.append(("clear", uid)) or 0)
    app = make_settings_app(ui, monkeypatch)
    app.start_fetch = lambda games=None: rec.fetches.append(games)
    rec.app = app
    return rec


def _open_search(ui, rec, game):
    StandInWidget.created = []
    StandInWidget.pack_order = []
    ui.dialogs.open_sgdb_search(rec.app, game)
    rec.threads.run_all()          # the autocomplete worker fills the list
    widgets = list(StandInWidget.created)
    listbox = [w for w in widgets if type(w).__name__ == "Listbox"][0]
    return widgets, listbox


def test_search_select_clears_that_game_and_fetches_only_it(ui, search_env):
    """B1 + Q1: Select saves the picked name, unskips and clears the game's art
    through the shared clear_game_art (snapshot + exact-id delete), then fetches
    ONLY that game."""
    game = _game(12345, name="Doom")
    widgets, listbox = _open_search(ui, search_env, game)
    listbox.selection_set(1)

    widget_by_text(widgets, "Select").opts["command"]()

    assert search_env.calls == [("id", 12345, 1993), ("name", 12345, "DOOM"),
                                ("unskip", 12345), ("clear", 12345)], (
        "the SGDB id of the row picked (DOOM 1993, not 2016) must be stored")
    assert search_env.fetches == [[game]], "Search → Select fetches only the matched game"


def test_search_select_refuses_while_a_fetch_runs(ui, search_env):
    search_env.app._fetch_busy = True
    widgets, listbox = _open_search(ui, search_env, _game(12345))
    listbox.selection_set(0)

    widget_by_text(widgets, "Select").opts["command"]()

    assert search_env.calls == [] and search_env.fetches == []
    status = [w for w in widgets if w.opts.get("text") == ui.fetch_mixin.FetchMixin.BUSY_MESSAGE]
    assert status, "the dialog's own status line must say a fetch is in progress"


# ---------------------------------------------------------------------------
# T6 / B9 — on-demand previews are filed by option index
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, body=b""):
        self.status_code = status
        self.content = body
        self.headers = {"Content-Length": str(len(body))}

    def iter_content(self, n):
        yield self.content


@pytest.fixture
def preview_env(ui, tmp_path, monkeypatch):
    im = ui.imaging
    pool = _ManualPool()
    monkeypatch.setattr(im, "_FETCH_POOL", pool)
    shown = []
    monkeypatch.setattr(im, "_display_image_on_label",
                        lambda lbl, path, on_anim=None, cache=None, cache_key=None,
                        on_shown=None: shown.append(path))
    bodies = {}
    monkeypatch.setattr(sgdb, "http_get", lambda url, **kw: bodies[url])
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(3):
        (cache / f"7p_{i}.png").write_bytes(b"cached %d" % i)
    state = {"index": 0,
             "paths": {i: str(cache / f"7p_{i}.png") for i in range(3)},
             "cache_base": str(cache / "7p"),
             "option_urls": [f"https://x/opt{i}.png" for i in range(5)],
             "preview_urls": [f"https://x/opt{i}.png" for i in range(5)],
             "option_meta": [{} for _ in range(5)]}
    return types.SimpleNamespace(state=state, pool=pool, shown=shown,
                                 bodies=bodies, cache=cache, img=StandInWidget(),
                                 counter=StandInWidget())


def _view(ui, env, index):
    env.state["index"] = index
    ui.imaging.update_view(env.state, env.img, env.counter)


def test_previews_finishing_out_of_order_still_map_to_their_options(ui, preview_env):
    """B9: ▶ to option 4 (index 3), ▶ again to 5 (index 4); 5's download lands
    first. Each preview must be filed under its own index, so ◀ back to 4 shows
    option 4 — the option Apply installs."""
    env = preview_env
    env.bodies.update({"https://x/opt3.png": _Resp(200, b"option 4"),
                       "https://x/opt4.png": _Resp(200, b"option 5")})
    _view(ui, env, 3)
    _view(ui, env, 4)
    assert len(env.pool.jobs) == 2
    env.pool.finish(1)                             # option 5 finishes first
    env.img.pump()
    env.pool.finish(0)
    env.img.pump()

    assert env.state["paths"][3].endswith("7p_3.png")
    assert env.state["paths"][4].endswith("7p_4.png")
    assert open(env.state["paths"][3], "rb").read() == b"option 4"
    assert open(env.state["paths"][4], "rb").read() == b"option 5"
    env.shown.clear()
    _view(ui, env, 3)                              # ◀ back to option 4
    assert env.shown == [env.state["paths"][3]]


def test_a_non_200_preview_writes_no_file(ui, preview_env):
    env = preview_env
    env.bodies["https://x/opt3.png"] = _Resp(404, b"<html>not found</html>")
    _view(ui, env, 3)
    env.pool.finish_all()
    env.img.pump()
    assert 3 not in env.state["paths"]
    assert sorted(os.listdir(env.cache)) == ["7p_0.png", "7p_1.png", "7p_2.png"], (
        "an error page must not be cached as an image")
    assert env.img.opts.get("text") == "preview\nunavailable"


def test_revisiting_an_option_while_it_downloads_starts_no_second_download(ui, preview_env):
    env = preview_env
    env.bodies["https://x/opt3.png"] = _Resp(200, b"option 4")
    _view(ui, env, 3)
    _view(ui, env, 2)
    _view(ui, env, 3)
    assert len(env.pool.jobs) == 1


# ---------------------------------------------------------------------------
# T-A4 — the results window owns its state; the poll/apply race guard holds
# ---------------------------------------------------------------------------

def test_main_poll_holds_off_while_the_results_apply_flow_runs(ui, results_env, monkeypatch):
    """Icons were queued (Steam open). The user presses "Close Steam & Apply
    Icons" in the results window; while that flow runs, the main window's 4 s
    pending-icon poll must not apply the queue too (they would race over the
    file). After the flow ends, the poll works again."""
    app = results_env.app
    world = types.SimpleNamespace(steam=True, pending={7: "/grid/7_icon.png"}, applied=[])
    monkeypatch.setattr(steam_env, "is_steam_running", lambda *a, **k: world.steam)
    monkeypatch.setattr(steam_files, "has_pending_icons", lambda: bool(world.pending))
    monkeypatch.setattr(steam_files, "load_pending_icons", lambda: dict(world.pending))
    monkeypatch.setattr(steam_env, "stop_steam", lambda: setattr(world, "steam", False))

    def apply_pending():
        world.applied.append(dict(world.pending))
        n = len(world.pending)
        world.pending = {}
        return n
    monkeypatch.setattr(steam_files, "apply_pending_icons", apply_pending)

    view = ui.results_window.ResultsWindow(app)
    app._results_view = view
    StandInWidget.created = []
    view.show([{"name": "G", "app_id": 7, "results": _fetched_art({
        "icons": _slot_data("7_icon", ["https://x/i.png"], [False], applied_index=0)})}])
    widgets = list(StandInWidget.created)

    widget_by_text(widgets, "Close Steam & Apply Icons").opts["command"]()
    assert view.is_applying() is True
    world.steam = False            # Steam is closing; the main poll ticks now
    app._flush_pending_icons()
    assert world.applied == [], "the main poll must hold off while the results flow runs"

    results_env.threads.run_all()  # the flow's worker: close Steam, apply, on_success
    assert world.applied == [{7: "/grid/7_icon.png"}]
    assert view.is_applying() is False

    world.pending = {8: "/grid/8_icon.png"}
    app._flush_pending_icons()
    assert world.applied[-1] == {8: "/grid/8_icon.png"}, "the poll works again afterwards"


def test_results_window_state_lives_on_the_window_not_the_app(ui, results_env):
    app = results_env.app
    view = ui.results_window.ResultsWindow(app)
    view.show([])
    for name in ("_results_widgets", "_results_applying", "_queued_apply_btns",
                 "_results_steam_state", "results_window", "_build_pending_icons"):
        assert not hasattr(app, name), f"app.{name} must not exist (T-A4)"
    assert view.widgets["window"] is view.top



def test_app_module_imports_and_composes_every_mixin(ui):
    """The whole import chain of app.py loads (no missing or circular import
    after the T-A5 split), SteamArtApp composes the four mixins, and startup
    calls steam_env.init() before the window. Under the stand-in tkinter: this
    proves imports and structure, not a real window."""
    import app
    assert [c.__name__ for c in app.SteamArtApp.__mro__[1:5]] == [
        "UIMixin", "GeometryMixin", "LibraryMixin", "FetchMixin"]
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    main = src[src.index('if __name__ == "__main__":'):]
    assert main.index("steam_env.init()") < main.index("window = tk.Tk()")


# ---------------------------------------------------------------------------
# T7 / B11 — one grid-folder listing per list render
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sort_key", ["name", "fetched"])
def test_a_list_render_lists_the_grid_folder_once(ui, fetch_env, monkeypatch, tmp_path, sort_key):
    """B11: every row's thumbnail (and the "Recently fetched" sort) listed the
    grid folder again — 300+ listings per debounced keystroke on a big library.
    The render lists it once and every row picks from that listing."""
    import steam_env
    grid = tmp_path / "grid"
    grid.mkdir()
    games = [_game(1000 + i, has_art=(i % 2 == 0)) for i in range(40)]
    for g in games[::2]:
        (grid / f"{g['app_id']}p.png").write_bytes(b"x")
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(grid))
    real_listdir = os.listdir
    listings = []

    def counting_listdir(path="."):
        if os.path.abspath(str(path)) == str(grid):
            listings.append(path)
        return real_listdir(path)
    monkeypatch.setattr(os, "listdir", counting_listdir)

    app = make_settings_app(ui, monkeypatch)
    app.games = [dict(g) for g in games]
    app.list_frame = StandInWidget()
    app.list_canvas = StandInWidget()
    app.search_var = StandInWidget(value="")
    app._sort_key, app._sort_desc = sort_key, False
    app.selected_row = app.selected_btn = None
    app._row_thumb_cache = {}
    app.ROW_THUMB_W, app.ROW_THUMB_H = 39, 59
    app._thumb_executor = _ManualPool()
    app.window.winfo_screenheight = lambda: 800
    StandInWidget.created = []

    ui.library_mixin.LibraryMixin._render_list(app)

    assert len(listings) == 1, f"the grid folder was listed {len(listings)} times for one render"
    # 20 games have a cover: each got a thumbnail box (a Frame holding a Label).
    labels_with_rows = [w for w in StandInWidget.created if type(w).__name__ == "Label"
                        and str(w.opts.get("text", "")).startswith("  Game")]
    assert len(labels_with_rows) == 40


# ---------------------------------------------------------------------------
# T8 / B12 — bounded image pool, pre-decoded frames, workers never touch Tk
# ---------------------------------------------------------------------------

def test_the_image_pool_never_runs_more_than_its_workers(ui):
    import threading
    import time as _time
    pool = ui.imaging._DaemonPool(2)
    running, peak, lock = [0], [0], threading.Lock()

    def job(i):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        _time.sleep(0.02)
        with lock:
            running[0] -= 1
        return i * i
    futures = [pool.submit(job, i) for i in range(10)]
    assert [f.result(timeout=5) for f in futures] == [i * i for i in range(10)]
    assert len(pool._threads) == 2 and peak[0] <= 2
    assert all(t.daemon for t in pool._threads), "a stalled job must not hold the app open"


class _ThreadCheckedLabel(StandInWidget):
    """Records the thread of every Tk-side call made on it."""
    def __init__(self, calls):
        super().__init__()
        self._calls = calls

    def _note(self, what):
        import threading
        self._calls.append((what, threading.get_ident()))

    def config(self, **kw):
        self._note("config")
        super().config(**kw)

    def after(self, ms, fn=None, *args):
        self._note("after")
        return super().after(ms, fn, *args)

    def after_cancel(self, ident):
        self._note("after_cancel")

    def winfo_exists(self):
        self._note("winfo_exists")
        return True


@pytest.mark.parametrize("animated", [False, True])
def test_decodes_run_on_the_pool_and_only_the_tk_thread_touches_tk(ui, tmp_path, monkeypatch, animated):
    """B12: the decode (and, for an animation, every frame's seek + thumbnail)
    runs on a pool worker; every widget call and every PhotoImage happens on the
    Tk (test) thread; a playing animation never resizes on the Tk thread."""
    import threading
    import time as _time
    from PIL import Image
    im = ui.imaging
    main = threading.get_ident()
    path = str(tmp_path / ("a.webp" if animated else "s.png"))
    if animated:
        frames = [Image.new("RGBA", (600, 400), (i * 40, 0, 0, 255)) for i in range(5)]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=50, loop=0)
    else:
        Image.new("RGB", (600, 400), (0, 90, 0)).save(path)

    calls, photos, thumbs = [], [], []

    class FakePhoto:
        def __init__(self, image):
            photos.append(threading.get_ident())
            self._size = image.size

        def width(self):
            return self._size[0]

        def height(self):
            return self._size[1]
    monkeypatch.setattr(im.ImageTk, "PhotoImage", FakePhoto)
    real_thumbnail = Image.Image.thumbnail

    def spy_thumbnail(self, *a, **k):
        thumbs.append(threading.get_ident())
        return real_thumbnail(self, *a, **k)
    monkeypatch.setattr(Image.Image, "thumbnail", spy_thumbnail)
    monkeypatch.setattr(im, "_DECODE_POOL", im._DaemonPool(2))   # real worker threads
    lbl = _ThreadCheckedLabel(calls)
    seen = []

    im._display_image_on_label(lbl, path, on_animated=seen.append)
    deadline = _time.monotonic() + 5
    while not photos and _time.monotonic() < deadline:
        _time.sleep(0.01)
        lbl.pump(rounds=1)
    lbl.pump(rounds=8)   # an animation keeps ticking: several frames shown

    assert seen == [animated]
    assert photos and set(photos) == {main}, "PhotoImage only on the Tk thread"
    assert calls and {t for _w, t in calls} == {main}, "widget calls only on the Tk thread"
    assert thumbs and main not in thumbs, "every resize happens on a pool worker"
    if animated:
        assert len(photos) >= 5, "the animation played its pre-decoded frames"



def test_settings_clear_all_artwork_reports_files_it_could_not_remove(ui, fetch_env, monkeypatch):
    """B14: a Clear that could not remove every file says so, in the log."""
    monkeypatch.setattr(ui.settings_window, "clear_managed_artwork", lambda: (3, 1))
    app = make_settings_app(ui, monkeypatch)
    widgets = open_settings_headless(ui, app)

    widget_by_text(widgets, "Clear All Artwork").opts["command"]()

    assert any(m.startswith("Cleared 3 artwork file(s) added by NonSteamScraper — 1 could "
                            "not be removed") for m in app.logged), app.logged


def test_settings_clear_cache_reports_files_it_could_not_remove(ui, fetch_env, monkeypatch):
    monkeypatch.setattr(ui.settings_window, "clear_cache", lambda: 2)
    app = make_settings_app(ui, monkeypatch)
    widgets = open_settings_headless(ui, app)

    widget_by_text(widgets, "Clear Cache").opts["command"]()

    assert "Cache cleared — 2 file(s) could not be removed." in app.logged



def test_search_dialog_buttons_cannot_be_pushed_out_by_a_long_list(ui, search_env):
    """Walk finding: with 10 results the list pushed Select / Cancel out of the
    window. The button bar is packed at the BOTTOM before the list (so the list
    takes only the space left), and the buttons sit on the left (ruling W7)."""
    widgets, listbox = _open_search(ui, search_env, _game(12345))
    select = widget_by_text(widgets, "Select")
    cancel = widget_by_text(widgets, "Cancel")
    bar = select.args[0]
    assert cancel.args[0] is bar
    list_frame = [w for w in widgets if type(w).__name__ == "Scrollbar"][0].args[0]
    order = StandInWidget.pack_order
    assert bar.packed.get("side") == "bottom"
    assert order.index(bar) < order.index(list_frame), (
        "the button bar must be packed before the list, or the list can push it out")
    assert select.packed.get("side") == "left" and cancel.packed.get("side") == "left"
    assert order.index(select) < order.index(cancel), "Select first, then Cancel"


# ---------------------------------------------------------------------------
# N2 — the pending-icon poll says when its write failed, once
# ---------------------------------------------------------------------------

def test_the_icon_poll_reports_a_failed_write_once_and_recovers(ui, fetch_env, monkeypatch):
    app = make_app(ui)
    result = [None]
    monkeypatch.setattr(steam_files, "has_pending_icons", lambda: True)
    monkeypatch.setattr(steam_files, "apply_pending_icons", lambda: result[0])
    app._flush_pending_icons()
    app._flush_pending_icons()   # the 4 s poll retrying
    failures = [m for m in app.logged if "Couldn't write the queued icons" in m]
    assert len(failures) == 1, app.logged
    assert not any(m.startswith("Applied") for m in app.logged)
    result[0] = 1
    app._flush_pending_icons()
    assert app.logged[-1].startswith("Applied 1 queued icon(s)")


# ---------------------------------------------------------------------------
# N9 — Undo says which games it could not put back
# ---------------------------------------------------------------------------

def test_undo_reports_games_it_could_not_put_back(ui, fetch_env, monkeypatch):
    app = make_app(ui)
    monkeypatch.setattr(restore, "undo_restore_point", lambda: {
        "games": 0, "files": 0, "removed": 0, "queued": False, "failed": 2,
        "icon_write_failed": False})
    app.undo_fetch()
    assert not any("Nothing to undo" in m for m in app.logged)
    assert any("could not put back the art of 2 game(s)" in m for m in app.logged)


# ---------------------------------------------------------------------------
# N4 — an "Apply this one" in flight counts as busy
# ---------------------------------------------------------------------------

def _start_apply(env, grid):
    """Press Apply on option 2 of a cover slot whose art lives in `grid`; the
    worker is recorded, not run. Returns the button and the target path."""
    data = _slot_data("7p", ["https://x/a.png", "https://x/b.jpg"], [False, False],
                      applied_index=0)
    data["applied_path"] = str(grid / "7p.png")
    widgets = build_results_section(env, 7, {"grids": data})
    [w for w in widgets if w.opts.get("text") == "▶"][0].opts["command"]()
    btn = _apply_button(widgets)
    btn.opts["command"]()
    return btn, str(grid / "7p.jpg")


def test_write_commands_refuse_while_an_apply_is_in_flight(ui, results_env, tmp_path, monkeypatch):
    """Apply, Back, Undo: the late Apply used to land on top of the Undo."""
    app = results_env.app
    undone = []
    monkeypatch.setattr(restore, "undo_restore_point", lambda: undone.append(1) or {
        "games": 1, "files": 1, "removed": 0, "queued": False, "failed": 0,
        "icon_write_failed": False})
    monkeypatch.setattr(ui.ui_mixin, "save_theme", lambda n: undone.append("theme"))
    monkeypatch.setattr(app, "relaunch_app", lambda: undone.append("relaunch"))
    _start_apply(results_env, tmp_path)

    app.undo_fetch()
    app.toggle_theme()
    app.start_fetch()
    assert undone == [] and app._fetch_busy is False
    assert app.status[-1] == app.APPLY_BUSY_MESSAGE

    results_env.threads.run_all()          # the apply ends
    assert app._apply_targets == []
    app.undo_fetch()
    assert undone == [1]


def test_closing_during_an_apply_waits_for_it(ui, results_env, tmp_path):
    """All Done / the X button / a relaunch used to kill the download mid-way."""
    app = results_env.app
    _start_apply(results_env, tmp_path)
    app._on_close()
    assert not app.window.destroyed
    assert app.status[-1].startswith("Finishing the artwork apply")
    assert [ms for ms, _fn in app.window.delayed] == [app.CLOSE_FALLBACK_MS]
    results_env.threads.run_all()
    assert app.window.destroyed, "the window closes once the apply has finished"


def test_the_close_fallback_removes_the_applys_unfinished_download(ui, results_env, tmp_path):
    app = results_env.app
    _btn, target = _start_apply(results_env, tmp_path)
    (tmp_path / "7p.jpg.part").write_bytes(b"half")
    (tmp_path / "7p.png").write_bytes(b"art")
    app._on_close()
    app._finish_close()                     # CLOSE_FALLBACK_MS ran out
    assert sorted(os.listdir(tmp_path)) == ["7p.png"]


def test_all_done_closes_through_the_same_wait(ui, results_env, tmp_path):
    app = results_env.app
    StandInWidget.created = []
    results_env.view.show([])
    done = widget_by_text(list(StandInWidget.created), "All Done!")
    _start_apply(results_env, tmp_path)
    done.opts["command"]()
    assert not app.window.destroyed
    results_env.threads.run_all()
    assert app.window.destroyed


def test_settings_opened_during_an_apply_comes_back_when_it_ends(ui, results_env, tmp_path):
    app = results_env.app
    _start_apply(results_env, tmp_path)
    widgets = open_settings_headless(ui, app)
    notes = [w for w in widgets
             if str(w.opts.get("text", "")).startswith("Artwork is being applied —")]
    assert len(notes) == 1
    # One Steam account here, so the account menu is the always-disabled placeholder.
    buttons = [w for w in _write_controls(widgets) if type(w).__name__ == "Button"]
    assert all(w.opts.get("state") == "disabled" for w in buttons)
    results_env.threads.run_all()
    assert all(w.opts.get("state") == "normal" for w in buttons)


# ---------------------------------------------------------------------------
# N8 — a failed preview shows no picture, never the previous option's
# ---------------------------------------------------------------------------

def test_a_failed_on_demand_preview_clears_the_previous_picture(ui, preview_env):
    """The label still showed option 1's image while the counter said 4/5 —
    Tk shows a label's image, not its text — and Apply then installed option 4."""
    env = preview_env
    env.img.opts["image"] = "option-1-photo"
    env.bodies["https://x/opt3.png"] = _Resp(404, b"<html>not found</html>")
    _view(ui, env, 3)
    env.pool.finish_all()
    env.img.pump()
    assert env.img.opts["image"] == "" and env.img.opts["text"] == "preview\nunavailable"
    assert env.counter.opts["text"] == "4 / 5"


def test_an_unreadable_preview_clears_the_previous_picture(ui, monkeypatch, tmp_path):
    im = ui.imaging
    pool = _ManualPool()
    monkeypatch.setattr(im, "_DECODE_POOL", pool)
    lbl = StandInWidget(image="option-1-photo")
    (tmp_path / "broken.png").write_bytes(b"not an image")
    im._display_image_on_label(lbl, str(tmp_path / "broken.png"))
    pool.finish_all()
    lbl.pump()
    assert lbl.opts["image"] == "" and lbl.opts["text"] == "preview\nunavailable"


def test_a_preview_that_failed_during_the_fetch_is_fetched_when_shown(ui, results_env):
    """Only previews that downloaded are recorded; option 1's failed one is
    downloaded again when the results card shows it, not shown stale."""
    data = _slot_data("7p", ["https://x/a.png", "https://x/b.png"], [False, False], 0)
    data["thumb_paths"] = [None, "/cache/7p_1.png"]
    pool = _ManualPool()
    ui.imaging._FETCH_POOL, saved = pool, ui.imaging._FETCH_POOL
    try:
        build_results_section(results_env, 7, {"grids": data})
    finally:
        ui.imaging._FETCH_POOL = saved
    assert len(pool.jobs) == 1
    _future, fn, args = pool.jobs[0]
    assert args[:2] == ("https://x/a.png", "/cache/7p_0.png")


# ---------------------------------------------------------------------------
# N11 — a tooltip can never be orphaned on screen
# ---------------------------------------------------------------------------

class _BindingWidget(StandInWidget):
    """A widget that keeps its bindings so a test can fire them. `inside` says
    whether the real pointer is over it (winfo_containing)."""
    inside = True

    def winfo_pointerxy(self):
        return (10, 10)

    def winfo_containing(self, x, y):
        return self if self.inside else None

    def bind(self, seq, fn, add=None):
        self.opts.setdefault("_bind", {}).setdefault(seq, [])
        if add != "+":
            self.opts["_bind"][seq] = []
        self.opts["_bind"][seq].append(fn)

    def fire(self, seq):
        for fn in self.opts.get("_bind", {}).get(seq, []):
            fn(types.SimpleNamespace(x_root=10, y_root=10))


def _tips():
    return [w for w in StandInWidget.created if type(w).__name__ == "Toplevel"]


def test_a_second_enter_without_a_leave_leaves_one_tooltip(ui):
    """The operator's report: the sort ▲/▼ tooltip stuck until restart. Closing
    the Sort menu sends the button a second <Enter> with no <Leave> between."""
    app = make_app(ui)
    StandInWidget.created = []
    btn = _BindingWidget()
    app._add_tooltip(btn, "Ascending")
    btn.fire("<Enter>")
    btn.fire("<Enter>")
    assert [t.destroyed for t in _tips()] == [True, False], "the first tip was orphaned"
    btn.fire("<Leave>")
    assert all(t.destroyed for t in _tips())


@pytest.mark.parametrize("seq", ["<ButtonPress>", "<Unmap>", "<Destroy>"])
def test_a_click_an_unmap_or_a_destroy_hides_the_tooltip(ui, seq):
    app = make_app(ui)
    StandInWidget.created = []
    btn = _BindingWidget()
    app._add_tooltip(btn, "Reload")
    btn.fire("<Enter>")
    btn.fire(seq)
    assert all(t.destroyed for t in _tips())


def test_the_tooltip_text_is_read_again_after_a_click(ui):
    app = make_app(ui)
    StandInWidget.created = []
    state = {"desc": False}
    btn = _BindingWidget()
    app._add_tooltip(btn, lambda: "Descending" if state["desc"] else "Ascending")
    btn.fire("<Enter>")
    btn.fire("<ButtonPress>")
    state["desc"] = True
    btn.fire("<Enter>")
    labels = [w.opts.get("text") for w in StandInWidget.created if type(w).__name__ == "Label"]
    assert labels == ["Ascending", "Descending"]


# ---------------------------------------------------------------------------
# N12 — a damaged shortcuts.vdf: an empty list and a message, not a crash
# ---------------------------------------------------------------------------

def test_an_unreadable_shortcuts_file_shows_a_message_instead_of_crashing(
        ui, fetch_env, tmp_path, monkeypatch):
    sc = tmp_path / "shortcuts.vdf"
    sc.write_bytes(b"\x00shortcuts\x00\x00\x01broken")       # half-written
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(sc))
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(tmp_path))
    monkeypatch.setattr(ui.library_mixin, "get_non_steam_games", steam_files.get_non_steam_games)
    app = make_app(ui, [_game(1)])

    assert app.load_games() is False
    assert app.games == []
    assert app.status[-1] == app.UNREADABLE_MESSAGE
    assert app.fetch_button.opts["state"] == "disabled"

    app.refresh_library()                     # Reload keeps the message up
    assert app.status[-1] == app.UNREADABLE_MESSAGE


# ---------------------------------------------------------------------------
# T11 — dialog hygiene (N13, N14, N16, N17, N18)
# ---------------------------------------------------------------------------

class _Named(StandInWidget):
    """A stand-in Toplevel with a Tk path name, recording transient()."""
    def __init__(self, path):
        super().__init__()
        self.path = path

    def __str__(self):
        return self.path

    def transient(self, parent):
        self.opts["transient"] = parent


def test_closing_a_window_takes_its_popups_off_the_modal_stack(ui):
    """N13: the results window closed during an animated conversion destroyed the
    progress popup with it, but the popup stayed on the stack; every later dialog
    then chained itself to a dead window."""
    app = make_app(ui)
    app._modal_stack = []
    results, popup = _Named(".!toplevel2"), _Named(".!toplevel2.!toplevel")
    app._open_modal(results)
    app._open_modal(popup)
    app._close_modal(results)
    results.destroy(); popup.destroy()
    assert app._modal_stack == []


def test_a_dead_window_is_never_a_new_dialogs_parent(ui):
    app = make_app(ui)
    dead = _Named(".!toplevel3")
    app._modal_stack = [dead]
    dead.destroy()
    new = _Named(".!toplevel4")
    app._open_modal(new)
    assert new.opts["transient"] is app.window
    assert app._modal_stack == [new]


def _search_with_traces(ui, monkeypatch, rec, game):
    """Open the Search dialog with a StringVar whose trace callbacks are kept, so
    a test can type into it. Returns (widgets, listbox, type_fn)."""
    sv_cls = ui.dialogs.tk.StringVar
    monkeypatch.setattr(sv_cls, "trace_add", lambda self, mode, cb, _c=None:
                        self.opts.setdefault("_traces", []).append(cb), raising=False)
    StandInWidget.created = []
    ui.dialogs.open_sgdb_search(rec.app, game)
    widgets = list(StandInWidget.created)
    var = [w for w in widgets if type(w).__name__ == "StringVar"][0]
    dlg = [w for w in widgets if type(w).__name__ == "Toplevel"][0]
    listbox = [w for w in widgets if type(w).__name__ == "Listbox"][0]

    def type_(text):
        var.set(text)
        for cb in var.opts.get("_traces", []):
            cb()
        dlg.pump()                     # the 300 ms typing timer fires
    return widgets, dlg, listbox, type_


def test_an_older_search_answer_never_overwrites_a_newer_one(ui, search_env, monkeypatch):
    rec = search_env
    answers = {"doom": (sgdb.SEARCH_FOUND, [{"id": 1, "name": "OLD", "year": "", "type": ""}]),
               "doom 2016": (sgdb.SEARCH_FOUND, [{"id": 2, "name": "NEW", "year": "", "type": ""}])}
    monkeypatch.setattr(ui.dialogs, "search_sgdb_autocomplete", lambda q: answers[q])
    widgets, dlg, listbox, type_ = _search_with_traces(ui, monkeypatch, rec, _game(1, "doom"))
    inserted = []
    listbox.insert = lambda where, text: inserted.append(text)
    type_("doom 2016")
    first, second = rec.threads.started
    rec.threads.started = []
    second.target(*second.args)        # the newer query answers first
    first.target(*first.args)          # then the older, slower one
    assert inserted == ["NEW"]


def test_a_search_that_answers_after_the_dialog_closed_touches_nothing(ui, search_env, monkeypatch):
    rec = search_env
    widgets, dlg, listbox, type_ = _search_with_traces(ui, monkeypatch, rec, _game(1, "doom"))
    inserted = []
    listbox.insert = lambda where, text: inserted.append(text)
    widget_by_text(widgets, "Cancel").opts["command"]()
    rec.threads.run_all()
    assert inserted == []


@pytest.mark.parametrize("outcome,text", [
    (sgdb.SEARCH_REFUSED, "SteamGridDB did not accept your API key — check it in Settings."),
    (sgdb.SEARCH_ERROR, "Couldn't reach SteamGridDB — check your connection and try again."),
    (sgdb.SEARCH_NOT_FOUND, "No results found"),
])
def test_the_search_dialog_tells_no_results_from_a_refused_key_or_no_network(
        ui, search_env, outcome, text):
    """N14: a refused key and no network both read "No results found"."""
    search_env.answer = (outcome, [])
    widgets, _listbox = _open_search(ui, search_env, _game(1, "doom"))
    assert any(w.opts.get("text") == text for w in widgets)


def test_search_sgdb_autocomplete_outcomes(monkeypatch):
    monkeypatch.setattr(appfiles, "load_api_key", lambda: "KEY")
    body = {"r": None}

    def fake(url, headers=None, params=None, timeout=None, retries=2):
        return body["r"]
    monkeypatch.setattr(sgdb, "_sgdb_get", fake)
    ok = types.SimpleNamespace(status_code=200, json=lambda: {"success": True, "data": [
        {"id": 7, "name": "Celeste", "types": ["steam"]}]})
    body["r"] = ok
    assert sgdb.search_sgdb_autocomplete("cel") == (sgdb.SEARCH_FOUND, [
        {"id": 7, "name": "Celeste", "year": "", "type": "steam"}])
    body["r"] = types.SimpleNamespace(status_code=200, json=lambda: {"success": True, "data": []})
    assert sgdb.search_sgdb_autocomplete("zzz") == (sgdb.SEARCH_NOT_FOUND, [])
    body["r"] = types.SimpleNamespace(status_code=401, json=lambda: {"success": False})
    assert sgdb.search_sgdb_autocomplete("cel") == (sgdb.SEARCH_REFUSED, [])
    body["r"] = types.SimpleNamespace(status_code=502, json=lambda: (_ for _ in ()).throw(ValueError()))
    assert sgdb.search_sgdb_autocomplete("cel") == (sgdb.SEARCH_ERROR, [])
    monkeypatch.setattr(appfiles, "load_api_key", lambda: "")
    assert sgdb.search_sgdb_autocomplete("cel") == (sgdb.SEARCH_REFUSED, [])


def test_a_list_redraw_binds_the_wheel_only_on_its_new_rows(ui):
    """N16: every redraw (each search keystroke) bound the session-long canvas and
    list frame again; tkinter keeps every bound callback's Tcl command until the
    widget is destroyed, so they piled up."""
    app = make_app(ui)
    rows = [StandInWidget(), StandInWidget()]
    app.list_frame = StandInWidget()
    app.list_frame.winfo_children = lambda: rows
    app.list_canvas = StandInWidget()
    app.window.winfo_screenheight = lambda: 800
    bound = []
    app._bind_wheel_to_canvas = lambda widget, canvas: bound.append(widget)
    app._size_list_canvas()
    assert bound == rows


def test_the_account_dropdown_list_follows_the_theme(ui, monkeypatch):
    """N17: the list that drops down opened light in dark mode."""
    app = make_settings_app(ui, monkeypatch, users=("111", "222"))
    widgets = open_settings_headless(ui, app)
    om = [w for w in widgets if type(w).__name__ == "OptionMenu"][0]
    menu = om["menu"]
    assert (menu.opts.get("bg"), menu.opts.get("fg")) == (app.theme["entry_bg"], app.theme["fg"])


def test_quick_start_and_art_preferences_are_placed_on_screen(ui, monkeypatch):
    """N18 + rulings Q16/Q16b: both are sized from their measured content (a typed
    500 px clipped the Options row), placed over the main window and kept on
    screen; their size is then fixed (no maximize button under KDE)."""
    app = make_settings_app(ui, monkeypatch)
    placed = []
    app._place_over_main = lambda win, w, h: placed.append((w, h)) or (w, h)
    monkeypatch.setattr(ui.dialogs, "load_prefs", lambda: dict(appfiles.DEFAULT_PREFS))
    for open_window in (ui.dialogs.show_info, ui.dialogs.open_art_prefs):
        StandInWidget.created = []
        resized = []
        tops = []
        real_toplevel = ui.dialogs.tk.Toplevel

        class Top(real_toplevel):
            def resizable(self, w, h):
                resized.append((w, h))

            def winfo_reqwidth(self):
                return 612
        monkeypatch.setattr(ui.dialogs.tk, "Toplevel", Top)
        open_window(app)
        canvases = [w for w in StandInWidget.created if type(w).__name__ == "Canvas"]
        assert canvases and canvases[0].opts.get("width") == 100, "the canvas asks for its body's width"
        assert resized[-1] == (False, False), "fixed size: no maximize button (Q16b)"
        monkeypatch.setattr(ui.dialogs.tk, "Toplevel", real_toplevel)
    assert [w for w, _h in placed] == [612, 612], "the measured width, not a typed one"


# ---------------------------------------------------------------------------
# N22 — the modal grab keeps trying while the dialog is not viewable yet
# ---------------------------------------------------------------------------

class _LateWindow(_BindingWidget):
    """A dialog the compositor maps late: grab_set fails `fails` times."""
    def __init__(self, ui, fails):
        super().__init__()
        self.fails = fails
        self._err = ui.geometry_mixin.tk.TclError
        self.grabs = 0

    def grab_set(self):
        if self.fails:
            self.fails -= 1
            raise self._err('grab failed: window "." not viewable')
        self.grabs += 1

    def title(self):
        return "Settings"


def test_a_late_mapping_dialog_still_becomes_modal(ui):
    app = make_app(ui)
    app._modal_stack = []
    win = _LateWindow(ui, fails=3)
    app._open_modal(win)
    win.pump()
    assert win.grabs == 1, "the grab gave up after one retry"


def test_a_dialog_becomes_modal_when_it_maps(ui):
    app = make_app(ui)
    app._modal_stack = []
    win = _LateWindow(ui, fails=1)
    app._open_modal(win)
    win.fire("<Map>")
    assert win.grabs == 1


def test_a_dialog_that_never_becomes_modal_says_so(ui, capsys):
    app = make_app(ui)
    app._modal_stack = []
    win = _LateWindow(ui, fails=1000)
    app._open_modal(win)
    win.pump()
    assert "Could not make the 'Settings' window modal" in capsys.readouterr().out
    assert app._modal_stack == [win]


def test_an_option_still_downloading_shows_no_previous_picture(ui, preview_env):
    """N8, the in-flight half: while option 4 downloads, option 1's picture stayed
    up under "4 / 5 (loading...)", and Apply installed option 4 unseen."""
    env = preview_env
    env.img.opts["image"] = "option-1-photo"
    env.bodies["https://x/opt3.png"] = _Resp(200, b"option 4")
    _view(ui, env, 3)
    assert env.img.opts["image"] == "" and env.img.opts["text"] == "loading..."


def test_a_finished_apply_draws_its_outcome_before_the_app_may_close(ui, results_env, tmp_path):
    app = results_env.app
    btn, _target = _start_apply(results_env, tmp_path)
    seen = []
    real = app._apply_finished
    app._apply_finished = lambda path: seen.append(btn.opts["text"]) or real(path)
    results_env.threads.run_all()
    assert seen == ["Applied!"]


# ---------------------------------------------------------------------------
# Options per artwork in Art Style Preferences (Q10, Q12)
# ---------------------------------------------------------------------------

def _art_prefs(ui, monkeypatch, saved):
    app = make_settings_app(ui, monkeypatch)
    app._place_over_main = lambda win, w, h: (w, h)
    monkeypatch.setattr(ui.dialogs, "load_prefs", lambda: dict(appfiles.DEFAULT_PREFS))
    monkeypatch.setattr(ui.dialogs, "save_prefs", saved.append)
    StandInWidget.created = []
    ui.dialogs.open_art_prefs(app)
    return list(StandInWidget.created)


def test_the_options_row_offers_1_3_5_7_10_and_saves_the_choice(ui, monkeypatch):
    saved = []
    widgets = _art_prefs(ui, monkeypatch, saved)
    buttons = {w.opts["text"]: w for w in widgets if type(w).__name__ == "Button"
               and w.opts.get("text") in ("1", "3", "5", "7", "10")}
    assert sorted(buttons, key=int) == ["1", "3", "5", "7", "10"]
    assert buttons["5"].opts["relief"] == "sunken", "5 is the default"
    buttons["3"].opts["command"]()
    widget_by_text(widgets, "Save & Close").opts["command"]()
    assert saved[-1]["options"] == "3"
    assert ui.mb.calls == [], "3 asks nothing"


def test_seven_or_ten_options_apply_without_a_question(ui, monkeypatch):
    """Ruling Q12-C: with thumbnail previews 7 and 10 cost little, so no box asks."""
    saved = []
    widgets = _art_prefs(ui, monkeypatch, saved)
    buttons = {w.opts["text"]: w for w in widgets if type(w).__name__ == "Button"
               and w.opts.get("text") in ("7", "10")}
    buttons["10"].opts["command"]()
    assert ui.mb.calls == []
    widget_by_text(widgets, "Save & Close").opts["command"]()
    assert saved[-1]["options"] == "10"


def test_art_preferences_reset_leaves_the_full_size_previews_setting(ui, monkeypatch):
    saved = []
    monkeypatch.setattr(appfiles, "DEFAULT_PREFS", dict(appfiles.DEFAULT_PREFS))
    app = make_settings_app(ui, monkeypatch)
    app._place_over_main = lambda win, w, h: (w, h)
    monkeypatch.setattr(ui.dialogs, "load_prefs",
                        lambda: dict(appfiles.DEFAULT_PREFS, full_previews="yes", options="3"))
    monkeypatch.setattr(ui.dialogs, "save_prefs", saved.append)
    StandInWidget.created = []
    ui.dialogs.open_art_prefs(app)
    widgets = list(StandInWidget.created)
    reset = [w for w in widgets if w.opts.get("text") == "Reset to defaults"][0]
    reset.opts["command"]()
    widget_by_text(widgets, "Save & Close").opts["command"]()
    assert saved[-1]["options"] == "5" and saved[-1]["full_previews"] == "yes"


def test_settings_full_size_previews_checkbox_saves_the_choice(ui, monkeypatch):
    stored = {"prefs": dict(appfiles.DEFAULT_PREFS)}
    monkeypatch.setattr(ui.settings_window, "load_prefs", lambda: dict(stored["prefs"]))
    monkeypatch.setattr(ui.settings_window, "save_prefs",
                        lambda p: stored.update(prefs=dict(p)))
    app = make_settings_app(ui, monkeypatch)
    widgets = open_settings_headless(ui, app)
    box = widget_by_text(widgets, "Full-size previews")
    assert type(box).__name__ == "Checkbutton"
    box.opts["variable"].set("yes")
    box.opts["command"]()
    assert stored["prefs"]["full_previews"] == "yes"
    assert stored["prefs"]["options"] == "5", "the other prefs are kept"


def test_an_on_demand_preview_downloads_the_preview_url_not_the_full_image(ui, preview_env):
    env = preview_env
    env.state["preview_urls"][3] = "https://x/thumb/opt3.jpg"
    env.bodies["https://x/thumb/opt3.jpg"] = _Resp(200, b"small")
    _view(ui, env, 3)
    env.pool.finish_all()
    env.img.pump()
    assert env.state["paths"][3] == str(env.cache / "7p_3.jpg")
    assert (env.cache / "7p_3.jpg").read_bytes() == b"small"



def test_a_crossing_event_with_the_pointer_elsewhere_shows_no_tooltip(ui):
    """The 1.5.3 walk: closing the results window released its grab, Tk sent the
    ▲/▼ button an <Enter> though the pointer was on the game list, and the tip
    stayed up — no <Leave> ever followed."""
    app = make_app(ui)
    StandInWidget.created = []
    btn = _BindingWidget()
    btn.inside = False
    app._add_tooltip(btn, "Ascending")
    btn.fire("<Enter>")
    assert _tips() == []


def test_a_tooltip_goes_when_the_pointer_leaves_without_a_leave_event(ui):
    app = make_app(ui)
    StandInWidget.created = []
    btn = _BindingWidget()
    app._add_tooltip(btn, "Ascending")
    btn.fire("<Enter>")
    btn.pump(rounds=2)
    assert [t.destroyed for t in _tips()] == [False], "kept while the pointer is on it"
    btn.inside = False
    btn.pump()
    assert all(t.destroyed for t in _tips())


# ---------------------------------------------------------------------------
# Round 5 — the Full-size previews warning (Q15) and window sizing (Q16, Q17)
# ---------------------------------------------------------------------------

_FULL_WARNING = ("Full-size previews download every option as the full image, so fetches "
                 "take much longer and use much more data.\n\nTurn on full-size previews?")


@pytest.mark.parametrize("answer,stored", [(True, "yes"), (False, "no")])
def test_ticking_full_size_previews_asks_first(ui, monkeypatch, answer, stored):
    prefs = {"p": dict(appfiles.DEFAULT_PREFS)}
    monkeypatch.setattr(ui.settings_window, "load_prefs", lambda: dict(prefs["p"]))
    monkeypatch.setattr(ui.settings_window, "save_prefs", lambda p: prefs.update(p=dict(p)))
    app = make_settings_app(ui, monkeypatch)
    widgets = open_settings_headless(ui, app)
    box = widget_by_text(widgets, "Full-size previews")
    ui.mb.answer = answer
    box.opts["variable"].set("yes")
    box.opts["command"]()
    assert ui.mb.calls[-1][0] == "askyesno"
    assert ui.mb.calls[-1][1] == ("Full-Size Previews", _FULL_WARNING)
    assert prefs["p"]["full_previews"] == stored
    assert box.opts["variable"].get() == stored, "No leaves it unticked"


def test_unticking_full_size_previews_asks_nothing(ui, monkeypatch):
    prefs = {"p": dict(appfiles.DEFAULT_PREFS, full_previews="yes")}
    monkeypatch.setattr(ui.settings_window, "load_prefs", lambda: dict(prefs["p"]))
    monkeypatch.setattr(ui.settings_window, "save_prefs", lambda p: prefs.update(p=dict(p)))
    app = make_settings_app(ui, monkeypatch)
    ui.mb.calls.clear()
    box = widget_by_text(open_settings_headless(ui, app), "Full-size previews")
    box.opts["variable"].set("no")
    box.opts["command"]()
    assert ui.mb.calls == [] and prefs["p"]["full_previews"] == "no"


def test_the_main_column_is_capped_and_centred_when_the_window_is_wide(ui):
    """Q17: maximized on 2560 px, rows used to span the whole screen."""
    app = make_app(ui)
    app.column = StandInWidget()
    app.column.pack_configure = lambda **kw: app.column.opts.update(kw)
    app._column_width = lambda: 1000
    app._center_column(types.SimpleNamespace(widget=app.window, width=2560))
    assert app.column.opts["padx"] == 780
    app._center_column(types.SimpleNamespace(widget=app.window, width=900))
    assert app.column.opts["padx"] == 0
    app._center_column(types.SimpleNamespace(widget=StandInWidget(), width=4000))
    assert app.column.opts["padx"] == 0, "a child's <Configure> is not the window's"


def test_a_capped_scrollable_body_is_centred_in_a_wide_canvas(ui):
    app = make_app(ui)
    app.theme = {"bg": "#000"}
    StandInWidget.created = []
    binds = {}
    calls = []
    real_canvas = ui.ui_mixin.tk.Canvas

    class Canvas(real_canvas):
        def bind(self, seq, fn, add=None):
            binds[seq] = fn

        def create_window(self, *a, **k):
            return "win1"

        def itemconfig(self, item, **kw):
            calls.append(("width", kw["width"]))

        def coords(self, item, x, y):
            calls.append(("x", x))
    ui.ui_mixin.tk.Canvas = Canvas
    try:
        app._make_scrollable_frame(StandInWidget(), max_width=1000)
    finally:
        ui.ui_mixin.tk.Canvas = real_canvas
    binds["<Configure>"](types.SimpleNamespace(width=2560))
    assert calls == [("width", 1000), ("x", 780)]
    calls.clear()
    binds["<Configure>"](types.SimpleNamespace(width=700))
    assert calls == [("width", 700), ("x", 0)]


@pytest.mark.parametrize("which", ["settings", "search"])
def test_settings_and_search_fit_their_content_and_keep_their_width(ui, monkeypatch, search_env, which):
    app = search_env.app
    fitted = []
    app._fit_dialog = lambda win, canvas=None, body=None, centre_on_screen=False: \
        fitted.append((canvas is not None, centre_on_screen))
    if which == "settings":
        open_settings_headless(ui, app)
        assert fitted == [(True, True)]
    else:
        _open_search(ui, search_env, _game(1, "Doom"))
        assert fitted == [(False, False)]


def test_a_short_main_window_never_loses_its_bottom_rows(ui, monkeypatch):
    """Seen 2026-10-20: the main window dragged to 700x520 pushed Fetch Missing
    Artwork out of sight — the rows were packed in reading order, and Tk's packer
    squeezes the last-packed widget first. Now the bottom rows are pinned to the
    bottom FIRST and the list is packed LAST, so the list is what shrinks."""
    app = make_settings_app(ui, monkeypatch)
    app.window = StandInWidget()
    app._refresh_onboarding = lambda: None
    app.ICON_TOOLBAR = 24
    app.open_art_prefs = app.show_info = lambda: None
    app._sort_key, app._sort_desc = "name", False
    StandInWidget.pack_order = []
    ui.library_mixin.LibraryMixin.build_ui(app)
    order = StandInWidget.pack_order
    list_box = app.list_canvas.args[0]
    bottom = [app.status_bar, app.log_toggle, app.undo_button, app.fetch_button]
    assert [order.index(w) for w in bottom] == sorted(order.index(w) for w in bottom), \
        "bottom-up: status line, Details, Undo, Fetch"
    assert all(w.packed.get("side") == "bottom" for w in bottom)
    assert order.index(list_box) > max(order.index(w) for w in bottom), "the list is packed last"
    assert list_box.packed.get("expand") is True


def test_the_progress_rows_sit_just_above_details(ui, monkeypatch):
    app = make_settings_app(ui, monkeypatch)
    app.progress_bar, app.progress_label = StandInWidget(), StandInWidget()
    app.log_toggle = StandInWidget()
    app._pack_progress()
    assert app.progress_bar.packed["after"] is app.log_toggle
    assert app.progress_label.packed["after"] is app.progress_bar


def test_the_main_window_cannot_be_made_shorter_than_its_controls(ui):
    """Seen 2026-10-20: with Details open, a 520 px window squeezed Fetch out of sight.
    The minimum height is every row but the list, plus two list rows."""
    app = make_app(ui)
    app.ROW_THUMB_H = 59
    app.column = types.SimpleNamespace(winfo_reqheight=lambda: 900)
    app.list_canvas = types.SimpleNamespace(winfo_reqheight=lambda: 300)
    app._get_workarea = lambda w: (0, 0, 1280, 1000)
    sizes = []
    app.window.minsize = lambda w, h: sizes.append((w, h))
    app._update_min_height()
    assert sizes == [(560, 600 + 2 * 65)]
    app._get_workarea = lambda w: (0, 0, 1280, 600)
    app._update_min_height()
    assert sizes[-1] == (560, 520), "never more than the screen allows"


# ---------------------------------------------------------------------------
# Round 6 — animated previews on demand (Q19/Q19b), the revisit spinner (Q21/Q21b)
# ---------------------------------------------------------------------------

def test_a_revisited_animated_preview_shows_a_turning_spinner_until_it_plays(ui, preview_env):
    """Q21/Q21b: going back to an animated option re-decodes it; the old picture
    stayed up with no sign of loading. Now: no picture, '(loading...)', a spinner."""
    env = preview_env
    env.state["option_meta"][1] = {"animated": True}
    env.img.opts["image"] = "option-1-photo"
    env.img._spinner = None      # a real label has no glyph yet (the stand-in answers any name)
    _view(ui, env, 1)
    assert env.img.opts["image"] == ""
    assert env.counter.opts["text"] == "2 / 5 (loading...)"
    assert env.img.opts["text"].endswith("Loading animated preview…")
    glyph = env.img._spinner
    assert glyph.opts["font"][1] == 30, "big glyph (Q21b)"
    first = glyph.opts["text"]
    env.img.pump(rounds=1)
    assert glyph.opts["text"] != first, "the spinner turns"
    env.img.opts["image"] = "frame-1"          # the decode put the first frame up
    env.img.pump()
    assert "_delayed" not in env.img.opts, "the spinner stopped by itself"


class _FakeLoader:
    def __init__(self):
        self.started, self.hidden = [], 0

    def start(self, index, url, cache_path, finish):
        self.started.append((index, url, cache_path))
        self.finish = finish

    def hide(self):
        self.hidden += 1


def test_an_animated_option_not_on_disk_goes_to_the_cards_download_panel(ui, preview_env):
    """Q19: animated previews are not fetched with the run; reaching one hands it to
    the card's panel (size, bar, Cancel) instead of a silent background download."""
    env = preview_env
    env.state["option_meta"][4] = {"animated_preview": True}
    env.state["preview_urls"][4] = "https://x/anim.webp"
    loader = env.state["animated_loader"] = _FakeLoader()
    env.img._spinner = None      # a real label has no glyph yet (the stand-in answers any name)
    _view(ui, env, 4)
    assert loader.started == [(4, "https://x/anim.webp", str(env.cache / "7p_4.webp"))]
    assert env.pool.jobs == [], "not the silent on-demand pool"
    assert env.counter.opts["text"] == "5 / 5", "the panel says what is happening"
    (env.cache / "7p_4.webp").write_bytes(b"anim")
    loader.finish(True)
    assert env.state["paths"][4] == str(env.cache / "7p_4.webp")
    # the decode after the download takes seconds too: the same spinner, never a blank box
    assert env.shown[-1] == str(env.cache / "7p_4.webp")
    assert env.counter.opts["text"] == "5 / 5 (loading...)"
    assert env.img.opts["text"].endswith("Loading animated preview…")
    assert env.img._spinner.opts["font"][1] == 30
    _view(ui, env, 0)
    assert loader.hidden >= 2, "the panel is hidden on every move"


def test_the_animated_download_panel_shows_progress_cancel_and_download_again(ui, monkeypatch):
    rw = ui.results_window
    pool = _ManualPool()
    monkeypatch.setattr(ui.imaging, "_ANIM_POOL", pool)
    progress_to_send = [(4_500_000, 18_700_000)]

    def fake_download(url, path, register=True, progress=None, cancel=None):
        for done, total in progress_to_send:
            progress(done, total)
        return not (cancel and cancel.is_set())
    monkeypatch.setattr(artwork, "download_artwork", fake_download)
    app = make_settings_app(ui, monkeypatch)
    loader = rw.AnimatedPreviewLoader(app, StandInWidget())
    finished = []

    # 1. reached: waiting, then the worker reports progress, then it lands
    loader.start(1, "https://x/a.webp", "/cache/7p_1.webp", lambda ok: finished.append(ok))
    assert loader.text.opts["text"] == "Waiting to download the animated preview…"
    job = loader.jobs[1]
    job.update(status="running", done=4_500_000, total=18_700_000)
    loader._draw()
    assert loader.text.opts["text"] == "Downloading the animated preview\n4.5 of 18.7 MB"
    assert round(loader.bar.opts["value"]) == 24 and loader.button.opts["text"] == "Cancel"
    pool.finish(0)
    loader.panel.pump()
    assert finished == [True] and loader.jobs == {}

    # 2. Cancel: no finish(), the panel offers Download; Download starts it again
    loader.start(2, "https://x/b.webp", "/cache/7p_2.webp", lambda ok: finished.append(ok))
    loader.jobs[2]["total"] = 28_700_000
    loader._press()
    assert loader.text.opts["text"] == "Download cancelled (28.7 MB)."
    assert loader.button.opts["text"] == "Download"
    pool.finish(1)
    loader.panel.pump()
    assert finished == [True], "a cancelled download never reaches finish()"
    loader._press()
    assert loader.jobs[2]["status"] == "waiting" and len(pool.jobs) == 3
