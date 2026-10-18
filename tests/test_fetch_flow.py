"""Behavioural tests for the fetch flow in the UI mixins, run headless.

fetch_mixin / library_mixin / geometry_mixin import tkinter, which needs a display
(and is not even installed in the dev venv). These tests load them under a stand-in
`tkinter` module (and stand-in widgets), then drive the real mixin methods on a fake
app object. Only the widget layer is faked: the fetch loop, the key check, the busy
state and the skip-list decisions all run for real.

The stand-in modules are installed into sys.modules for this file only and removed
again afterwards, so no other test can pick them up.

HOME is sandboxed by tests/conftest.py before find_games is imported (importing it
binds every ~/.steamart_* path to HOME).
"""

import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

assert os.path.basename(os.environ["HOME"]).startswith("steamart_test_home_"), (
    "tests/conftest.py must sandbox HOME before find_games is imported")

import find_games as fg  # noqa: E402
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

    def __init__(self, *args, **opts):
        self.args = args
        self.opts = dict(opts)
        self.destroyed = False
        StandInWidget.created.append(self)

    def config(self, **kw):
        self.opts.update(kw)
    configure = config

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

    def after(self, ms, fn=None, *args):
        # like FakeWindow: the Tk thread is the test thread; delayed calls never run
        if ms == 0 and fn is not None:
            fn(*args)
        return "after#"

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
               "PIL.ImageTk")


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
    no icons, no work-area probing. find_games reads are patched in settings_window."""
    from theming import resolve_theme
    sw = ui.settings_window
    monkeypatch.setattr(sw, "get_all_steam_users", lambda: list(users))
    monkeypatch.setattr(sw, "is_steam_running", lambda *a, **k: False)
    monkeypatch.setattr(sw, "get_cache_size", lambda: 1.5)
    monkeypatch.setattr(sw, "load_api_key", lambda: "KEY")
    monkeypatch.setattr(steam_env, "get_steam_user_personas", lambda: {})
    monkeypatch.setattr(steam_env, "get_active_user", lambda: users[0])
    monkeypatch.setattr(fg, "restore_point_size", lambda: 0.0)
    monkeypatch.setattr(steam_files, "legacy_backup_size", lambda: 0.0)
    app = make_app(ui)
    app.theme = resolve_theme("dark", "blue")
    app._icon_cache = {}
    app._modal_stack = []
    app.ICON_BTN = app.ICON_INLINE = app.ICON_ACTION = app.ICON_NAV = 24
    app._icon = lambda name, size=18: None
    app._fit_window_to_workarea = lambda win, desired_w, desired_h: (desired_w, desired_h)
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
    art = fg.FetchedArt()
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
        return fg.FetchedArt()

    # operations.run_fetch reads find_games live, so these are patched on fg.
    monkeypatch.setattr(sgdb, "search_game", fake_search)
    monkeypatch.setattr(fg, "download_all_artwork", fake_download)
    monkeypatch.setattr(appfiles, "add_to_skip_list", lambda app_id: rec.skipped.append(app_id))
    monkeypatch.setattr(fm, "load_prefs", lambda: {})
    monkeypatch.setattr(fm, "load_api_key", lambda: "KEY")
    monkeypatch.setattr(sgdb, "check_api_key", lambda key=None: sgdb.KEY_ACCEPTED)
    monkeypatch.setattr(fg, "begin_restore_run", lambda: rec.calls.append("begin"))
    monkeypatch.setattr(fg, "seal_restore_point", lambda: rec.calls.append("seal"))
    monkeypatch.setattr(steam_env, "is_steam_running", lambda *a, **k: False)
    monkeypatch.setattr(steam_files, "set_shortcut_icons", lambda m: len(m))
    monkeypatch.setattr(steam_files, "save_pending_icons", lambda m: None)
    monkeypatch.setattr(fg, "has_restore_point", lambda: True)
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
    monkeypatch.setattr(fg, "download_all_artwork", fake_download)
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
    monkeypatch.setattr(fg, "clear_game_art", lambda uid: done.append(("clear", uid)))
    monkeypatch.setattr(ui.library_mixin, "add_to_skip_list", lambda uid: done.append(("skip", uid)))
    monkeypatch.setattr(fg, "undo_restore_point", lambda: done.append("undo") or
                        {"games": 0, "files": 0, "removed": 0, "queued": False})
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
    monkeypatch.setattr(ui.imaging, "threading", _InlineThreads())   # decodes never run
    monkeypatch.setattr(rw, "_display_image_on_label", lambda *a, **k: None)
    rec = types.SimpleNamespace(installed=[], written=[], queued=[], threads=threads,
                                install_ok=True, steam_running=False)

    def fake_install(save_path, url, existing_files=None, animated=False,
                     progress_cb=None, status_cb=None):
        rec.installed.append((save_path, url, animated))
        return rec.install_ok
    monkeypatch.setattr(fg, "install_into_slot", fake_install)
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
            "option_urls": list(urls),
            "option_meta": [{"animated": a, "nsfw": False, "humor": False, "steam": False}
                            for a in animated],
            "thumb_paths": [f"/cache/{base}_{i}.png" for i in range(min(3, len(urls)))],
            "filename_base": base}


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
    monkeypatch.setattr(dl, "search_sgdb_autocomplete", lambda q: [
        {"id": 2016, "name": "DOOM", "year": "2016", "type": "game"},
        {"id": 1993, "name": "DOOM", "year": "1993", "type": "game"}])
    monkeypatch.setattr(appfiles, "save_id_override",
                        lambda uid, sid: rec.calls.append(("id", uid, sid)))
    monkeypatch.setattr(appfiles, "save_name_override",
                        lambda uid, name: rec.calls.append(("name", uid, name)))
    monkeypatch.setattr(appfiles, "remove_from_skip_list", lambda uid: rec.calls.append(("unskip", uid)))
    monkeypatch.setattr(fg, "clear_game_art", lambda uid: rec.calls.append(("clear", uid)) or 0)
    app = make_settings_app(ui, monkeypatch)
    app.start_fetch = lambda games=None: rec.fetches.append(games)
    rec.app = app
    return rec


def _open_search(ui, rec, game):
    StandInWidget.created = []
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
    threads = _InlineThreads()
    monkeypatch.setattr(im, "threading", threads)
    shown = []
    monkeypatch.setattr(im, "_display_image_on_label",
                        lambda lbl, path, on_anim=None, cache=None, cache_key=None:
                        shown.append(path))
    bodies = {}
    monkeypatch.setattr(sgdb, "http_get", lambda url, **kw: bodies[url])
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(3):
        (cache / f"7p_{i}.png").write_bytes(b"cached %d" % i)
    state = {"index": 0,
             "paths": {i: str(cache / f"7p_{i}.png") for i in range(3)},
             "option_urls": [f"https://x/opt{i}.png" for i in range(5)],
             "option_meta": [{} for _ in range(5)]}
    return types.SimpleNamespace(state=state, threads=threads, shown=shown,
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
    fetch_3, fetch_4 = env.threads.started
    env.threads.started[:] = [fetch_4, fetch_3]   # option 5 finishes first
    env.threads.run_all()

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
    env.threads.run_all()
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
    assert len(env.threads.started) == 1


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
