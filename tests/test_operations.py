"""Behavioural tests for operations.py — the app's rules with no tkinter.

These replace AST "wiring" tests that pinned WHERE a call sat in a UI file
(begin_restore_run before the fetch loop, seal after it, the elapsed time in the
log line, Re-fetch / Search → Select clearing through clear_game_art). With the
rules out of the UI they are run and observed instead.

HOME is sandboxed by tests/conftest.py before find_games is imported.
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

assert os.path.basename(os.environ["HOME"]).startswith("steamart_test_home_"), (
    "tests/conftest.py must sandbox HOME before find_games is imported")

import find_games as fg  # noqa: E402
import appfiles
import operations  # noqa: E402


# ---------------------------------------------------------------------------
# run_fetch — the order of a fetch run
# ---------------------------------------------------------------------------

@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """Record, in ONE ordered list, every step run_fetch takes outside itself."""
    rec = types.SimpleNamespace(calls=[], logs=[], progress=[], outcomes={},
                                icon_for=set(), steam_running=False, key=fg.KEY_ACCEPTED)

    def search(name, app_id=None):
        rec.calls.append(("search", app_id))
        return rec.outcomes.get(app_id, (fg.SEARCH_FOUND, 1000 + app_id))

    def download(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        rec.calls.append(("download", app_id))
        assert defer_icon is True, "the run batches icon writes"
        progress_cb("Downloading Cover", 1, 4)
        art = fg.FetchedArt()
        art.slots["grids"] = {"label": "Cover", "failed": False, "applied_index": 0,
                              "applied_path": f"/g/{app_id}p.png"}
        if app_id in rec.icon_for:
            art.icon_write = (app_id, f"/g/{app_id}_icon.png")
        return art

    monkeypatch.setattr(fg, "check_api_key",
                        lambda key=None: rec.calls.append("check") or rec.key)
    monkeypatch.setattr(fg, "begin_restore_run", lambda: rec.calls.append("begin"))
    monkeypatch.setattr(fg, "seal_restore_point", lambda: rec.calls.append("seal"))
    monkeypatch.setattr(fg, "search_game", search)
    monkeypatch.setattr(fg, "download_all_artwork", download)
    monkeypatch.setattr(appfiles, "add_to_skip_list", lambda uid: rec.calls.append(("skip", uid)))
    monkeypatch.setattr(fg, "is_steam_running", lambda *a, **k: rec.steam_running)
    monkeypatch.setattr(fg, "set_shortcut_icons",
                        lambda m: rec.calls.append(("write_icons", dict(m))) or len(m))
    monkeypatch.setattr(fg, "save_pending_icons",
                        lambda m: rec.calls.append(("queue_icons", dict(m))))
    rec.ids_file = tmp_path / "ids"
    monkeypatch.setattr(appfiles, "IDS_FILE", str(rec.ids_file))
    rec.on_log = lambda msg, icon: rec.logs.append(msg)
    rec.on_progress = lambda pct, label, status=None: rec.progress.append((pct, label, status))
    return rec


def _games(*ids):
    return [{"name": f"Game {i}", "app_id": i, "has_art": False, "skipped": False}
            for i in ids]


def _run(rec, games, stop_after=None):
    seen = []

    def should_stop():
        seen.append(1)
        return stop_after is not None and len(seen) > stop_after
    return operations.run_fetch(games, {}, rec.on_progress, rec.on_log, should_stop)


def test_run_fetch_with_nothing_to_fetch_touches_nothing(run_env):
    """A no-op run must not check the key, begin a restore run or seal — it would
    drop every sealed entry and erase the user's Undo."""
    report = _run(run_env, [])
    assert run_env.calls == []
    assert report["outcome"] == operations.FETCH_NOTHING
    assert run_env.logs == ["All games already have artwork!"]


def test_run_fetch_order_check_begin_games_seal_icons(run_env):
    """The key check, then begin_restore_run, then the games, then the seal, then
    ONE batched icon write — each before the next."""
    run_env.icon_for = {1, 2}
    report = _run(run_env, _games(1, 2))
    assert run_env.calls == [
        "check", "begin",
        ("search", 1), ("download", 1),
        ("search", 2), ("download", 2),
        "seal",
        ("write_icons", {1: "/g/1_icon.png", 2: "/g/2_icon.png"}),
    ]
    assert report["outcome"] == operations.FETCH_RAN
    assert [r["app_id"] for r in report["results"]] == [1, 2]
    assert report["stopped"] is None


def test_run_fetch_queues_icons_while_steam_runs(run_env):
    run_env.icon_for = {1}
    run_env.steam_running = True
    _run(run_env, _games(1))
    assert run_env.calls[-1] == ("queue_icons", {1: "/g/1_icon.png"})
    assert not any(c[0] == "write_icons" for c in run_env.calls if isinstance(c, tuple))


@pytest.mark.parametrize("key,outcome", [(fg.KEY_REJECTED, operations.FETCH_REJECTED),
                                         (fg.KEY_UNREACHABLE, operations.FETCH_UNREACHABLE)])
def test_run_fetch_stops_before_any_change_when_the_key_fails(run_env, key, outcome):
    run_env.key = key
    report = _run(run_env, _games(1, 2))
    assert run_env.calls == ["check"], "nothing may change on disk"
    assert report["outcome"] == outcome and report["results"] == []


def test_run_fetch_stop_flag_is_checked_between_games_and_still_seals(run_env):
    report = _run(run_env, _games(1, 2, 3), stop_after=1)
    assert [c for c in run_env.calls if isinstance(c, tuple)] == [("search", 1), ("download", 1)]
    assert "seal" in run_env.calls
    assert report["stopped"] == "Fetch stopped."
    assert run_env.progress[-1][1] == "Stopped — Fetched artwork for 1 of 3 game(s)."


def test_run_fetch_log_line_carries_the_per_game_elapsed_time(run_env, monkeypatch):
    clock = iter([100.0, 102.5])
    monkeypatch.setattr(operations.time, "monotonic", lambda: next(clock))
    _run(run_env, _games(7))
    assert "Artwork saved for Game 7 (2.5s)" in run_env.logs


def test_run_fetch_progress_reports_search_then_slot_steps(run_env):
    _run(run_env, _games(1, 2))
    assert run_env.progress[0] == (0.0, "[1/2] Game 1 — Searching SteamGridDB...",
                                   "Searching: Game 1")
    # slot step 1 of 4 inside game 1 (0..50 %): 12.5 %
    assert run_env.progress[1] == (12.5, "[1/2] Game 1 — Downloading Cover", None)
    assert run_env.progress[-1] == (100, "Done! Fetched artwork for 2 of 2 game(s).", None)


# ---------------------------------------------------------------------------
# refetch / rematch / restore_from_skip — on real files in tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture
def files(tmp_path, monkeypatch):
    grid = tmp_path / "grid"
    grid.mkdir()
    for f in ("12345p.png", "12345_hero.jpg", "123456p.png", "123456_logo.png"):
        (grid / f).write_bytes(f.encode())
    monkeypatch.setattr(fg, "GRID_FOLDER", str(grid))
    monkeypatch.setattr(fg, "SHORTCUTS_PATH", str(tmp_path / "shortcuts.vdf"))
    monkeypatch.setattr(appfiles, "RESTORE_FOLDER", str(tmp_path / "restore"))
    monkeypatch.setattr(appfiles, "MANAGED_FILE", str(tmp_path / "managed"))
    monkeypatch.setattr(appfiles, "SKIP_FILE", str(tmp_path / "skip"))
    monkeypatch.setattr(appfiles, "NAMES_FILE", str(tmp_path / "names"))
    monkeypatch.setattr(appfiles, "IDS_FILE", str(tmp_path / "ids"))
    (tmp_path / "skip").write_text("12345\n999\n", encoding="utf-8")
    return tmp_path


def _manifest(root):
    with open(root / "restore" / "manifest.json", encoding="utf-8") as f:
        return json.load(f)


def test_refetch_clears_only_that_game_snapshots_first_and_unskips(files):
    removed = operations.refetch(12345)
    assert removed == 2
    assert sorted(os.listdir(files / "grid")) == ["123456_logo.png", "123456p.png"], (
        "game 123456's art must survive a Re-fetch of 12345")
    entry = _manifest(files)["entries"]["12345"]
    assert sorted(entry["files"]) == ["12345_hero.jpg", "12345p.png"], (
        "Undo needs the snapshot taken BEFORE the delete")
    assert sorted(os.listdir(files / "restore" / "12345")) == ["12345_hero.jpg", "12345p.png"]
    assert (files / "skip").read_text(encoding="utf-8") == "999\n"


def test_rematch_saves_the_name_then_clears_like_refetch(files):
    operations.rematch(12345, "DOOM", 1993)
    assert appfiles.load_name_overrides() == {"12345": "DOOM"}
    assert appfiles.load_id_overrides() == {"12345": 1993}
    assert sorted(os.listdir(files / "grid")) == ["123456_logo.png", "123456p.png"]
    assert "12345" in _manifest(files)["entries"]
    assert (files / "skip").read_text(encoding="utf-8") == "999\n"


def test_restore_from_skip_touches_only_the_skip_list(files):
    operations.restore_from_skip(12345)
    assert (files / "skip").read_text(encoding="utf-8") == "999\n"
    assert len(os.listdir(files / "grid")) == 4


def test_apply_option_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(fg, "install_option", boom)
    assert operations.apply_option("/g/1p.png", "u", 1) == (False, False)


def test_run_fetch_uses_a_picked_sgdb_id_without_searching(run_env, monkeypatch):
    """T5 / B8: Search → Select stored SGDB id 1993 for game 2. The run fetches
    THAT id; a name search could land on a same-named game (DOOM 2016)."""
    run_env.ids_file.write_text("2|1993\n", encoding="utf-8")
    downloaded = []
    recorded_download = fg.download_all_artwork   # run_env's stand-in

    def record(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        downloaded.append((app_id, sgdb_id))
        return recorded_download(sgdb_id, app_id, prefs, progress_cb, defer_icon)
    monkeypatch.setattr(fg, "download_all_artwork", record)

    _run(run_env, _games(1, 2))

    assert ("search", 2) not in run_env.calls, "a picked id skips search_game"
    assert ("search", 1) in run_env.calls, "a game without a pick is searched as before"
    assert downloaded == [(1, 1001), (2, 1993)]
