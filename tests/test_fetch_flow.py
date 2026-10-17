"""Behavioural tests for the fetch flow in the UI mixins, run headless.

fetch_mixin / library_mixin / geometry_mixin import tkinter, which needs a display
(and is not even installed in the dev venv). These tests load them under a stand-in
`tkinter` module (and stand-in widgets), then drive the real mixin methods on a fake
app object. Only the widget layer is faked: the fetch loop, the key check, the busy
state and the skip-list decisions all run for real.

The stand-in modules are installed into sys.modules for this file only and removed
again afterwards, so no other test can pick them up.

Like test_find_games.py, HOME is sandboxed before find_games is imported (importing
it creates folders under HOME). If another test file imported it first, its sandbox
is already in place and is reused.
"""

import os
import sys
import types
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "find_games" not in sys.modules:
    _SANDBOX_HOME = tempfile.mkdtemp(prefix="steamart_test_home_")
    os.environ["HOME"] = _SANDBOX_HOME
    os.environ["USERPROFILE"] = _SANDBOX_HOME

import find_games as fg  # noqa: E402


# ---------------------------------------------------------------------------
# Stand-in tkinter
# ---------------------------------------------------------------------------

class _Dummy:
    """Any tkinter class the mixins name at import time or in an untested path."""
    def __init__(self, *a, **k):
        pass


def _fake_tk_modules():
    """Build the stand-in tkinter, tkinter.messagebox and tkinter.ttk modules.
    messagebox records every call in .calls and answers askyesno with .answer."""
    tk = types.ModuleType("tkinter")
    tk.__getattr__ = lambda name: _Dummy
    tk.TclError = type("TclError", (Exception,), {})
    mb = types.ModuleType("tkinter.messagebox")
    mb.calls = []
    mb.answer = True

    def _record(kind):
        def f(*a, **k):
            mb.calls.append((kind, a, k))
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
               "imaging", "PIL.ImageTk")


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
        yield types.SimpleNamespace(
            fetch_mixin=fetch_mixin, library_mixin=library_mixin,
            geometry_mixin=geometry_mixin, ui_mixin=ui_mixin,
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
        return rec.outcomes.get(app_id, (fg.SEARCH_FOUND, 4242))

    def fake_download(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        rec.downloaded.append(app_id)
        return {"icon_to_set": None}

    monkeypatch.setattr(fm, "search_game", fake_search)
    monkeypatch.setattr(fm, "download_all_artwork", fake_download)
    monkeypatch.setattr(fm, "add_to_skip_list", lambda app_id: rec.skipped.append(app_id))
    monkeypatch.setattr(fm, "load_prefs", lambda: {})
    monkeypatch.setattr(fm, "load_api_key", lambda: "KEY")
    monkeypatch.setattr(fg, "check_api_key", lambda key=None: fg.KEY_ACCEPTED)
    monkeypatch.setattr(fg, "begin_restore_run", lambda: rec.calls.append("begin"))
    monkeypatch.setattr(fg, "seal_restore_point", lambda: rec.calls.append("seal"))
    monkeypatch.setattr(fg, "is_steam_running", lambda *a, **k: False)
    monkeypatch.setattr(fg, "set_shortcut_icons", lambda m: len(m))
    monkeypatch.setattr(fg, "save_pending_icons", lambda m: None)
    monkeypatch.setattr(fg, "has_restore_point", lambda: True)
    monkeypatch.setattr(ui.library_mixin, "get_non_steam_games",
                        lambda: [dict(g) for g in rec.library])
    ui.mb.calls.clear()
    ui.mb.answer = True
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
    monkeypatch.setattr(fg, "check_api_key", lambda key=None: fg.KEY_REJECTED)
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
    monkeypatch.setattr(fg, "check_api_key", lambda key=None: fg.KEY_UNREACHABLE)
    app = make_app(ui, [_game(1)])

    app._run_fetch_body()

    assert "begin" not in fetch_env.calls
    assert fetch_env.searched == [] and fetch_env.skipped == []
    assert ui.mb.calls == [], "no popup when SteamGridDB cannot be reached"
    assert app.settings_opened == 0
    assert app.status[-1] == ("Couldn't reach SteamGridDB — nothing was fetched. "
                              "Check your connection and try again.")


def test_key_check_runs_before_begin_restore_run(ui, fetch_env, monkeypatch):
    monkeypatch.setattr(fg, "check_api_key",
                        lambda key=None: fetch_env.calls.append("check") or fg.KEY_ACCEPTED)
    app = make_app(ui, [_game(1)])

    app._run_fetch_body()

    assert fetch_env.calls[:2] == ["check", "begin"]


def test_refused_search_is_not_skip_listed_and_stops_the_run(ui, fetch_env):
    """Safety net (Q2): a refusal mid-run stops the loop at that game, hides
    nothing, and still seals the games already done."""
    fetch_env.outcomes = {2: (fg.SEARCH_REFUSED, None)}
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
    fetch_env.outcomes = {1: (fg.SEARCH_NOT_FOUND, None), 2: (fg.SEARCH_ERROR, None)}
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
        return dict(slots, icon_to_set=None)
    monkeypatch.setattr(ui.fetch_mixin, "download_all_artwork", fake_download)
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
    monkeypatch.setattr(app, "_remove_from_skip", lambda uid: done.append(("unskip", uid)))
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
    monkeypatch.setattr(fg, "GRID_FOLDER", str(grid))
    monkeypatch.setattr(fg, "CACHE_FOLDER", str(cache))
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
