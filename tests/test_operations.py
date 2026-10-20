"""Behavioural tests for operations.py — the app's rules with no tkinter.

These replace AST "wiring" tests that pinned WHERE a call sat in a UI file
(begin_restore_run before the fetch loop, seal after it, the elapsed time in the
log line, Re-fetch / Search → Select clearing through clear_game_art). With the
rules out of the UI they are run and observed instead.

HOME is sandboxed by tests/conftest.py before the backend is imported.
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

assert os.path.basename(os.environ["HOME"]).startswith("steamart_test_home_"), (
    "tests/conftest.py must sandbox HOME before the backend is imported")

import artwork
import restore
import steam_files
import sgdb
import steam_env
import appfiles
import operations  # noqa: E402


# ---------------------------------------------------------------------------
# run_fetch — the order of a fetch run
# ---------------------------------------------------------------------------

@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """Record, in ONE ordered list, every step run_fetch takes outside itself."""
    rec = types.SimpleNamespace(calls=[], logs=[], progress=[], outcomes={},
                                icon_for=set(), steam_running=False, key=sgdb.KEY_ACCEPTED)

    def search(name, app_id=None):
        rec.calls.append(("search", app_id))
        return rec.outcomes.get(app_id, (sgdb.SEARCH_FOUND, 1000 + app_id))

    def download(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        rec.calls.append(("download", app_id))
        assert defer_icon is True, "the run batches icon writes"
        progress_cb("Downloading Cover", 1, 4)
        art = artwork.FetchedArt()
        art.slots["grids"] = {"label": "Cover", "failed": False, "applied_index": 0,
                              "applied_path": f"/g/{app_id}p.png"}
        if app_id in rec.icon_for:
            art.icon_write = (app_id, f"/g/{app_id}_icon.png")
        return art

    monkeypatch.setattr(sgdb, "check_api_key",
                        lambda key=None: rec.calls.append("check") or rec.key)
    monkeypatch.setattr(restore, "begin_restore_run", lambda: rec.calls.append("begin"))
    monkeypatch.setattr(restore, "seal_restore_point", lambda: rec.calls.append("seal"))
    monkeypatch.setattr(sgdb, "search_game", search)
    monkeypatch.setattr(artwork, "download_all_artwork", download)
    monkeypatch.setattr(appfiles, "add_to_skip_list", lambda uid: rec.calls.append(("skip", uid)))
    monkeypatch.setattr(steam_env, "is_steam_running", lambda *a, **k: rec.steam_running)
    monkeypatch.setattr(steam_files, "set_shortcut_icons",
                        lambda m: rec.calls.append(("write_icons", dict(m))) or len(m))
    monkeypatch.setattr(steam_files, "save_pending_icons",
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


@pytest.mark.parametrize("key,outcome", [(sgdb.KEY_REJECTED, operations.FETCH_REJECTED),
                                         (sgdb.KEY_UNREACHABLE, operations.FETCH_UNREACHABLE)])
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
    monkeypatch.setattr(steam_env, "GRID_FOLDER", str(grid))
    monkeypatch.setattr(steam_env, "SHORTCUTS_PATH", str(tmp_path / "shortcuts.vdf"))
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
    monkeypatch.setattr(artwork, "install_option", boom)
    assert operations.apply_option("/g/1p.png", "u", 1) == (False, False)


def test_run_fetch_uses_a_picked_sgdb_id_without_searching(run_env, monkeypatch):
    """T5 / B8: Search → Select stored SGDB id 1993 for game 2. The run fetches
    THAT id; a name search could land on a same-named game (DOOM 2016)."""
    run_env.ids_file.write_text("2|1993\n", encoding="utf-8")
    downloaded = []
    recorded_download = artwork.download_all_artwork   # run_env's stand-in

    def record(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        downloaded.append((app_id, sgdb_id))
        return recorded_download(sgdb_id, app_id, prefs, progress_cb, defer_icon)
    monkeypatch.setattr(artwork, "download_all_artwork", record)

    _run(run_env, _games(1, 2))

    assert ("search", 2) not in run_env.calls, "a picked id skips search_game"
    assert ("search", 1) in run_env.calls, "a game without a pick is searched as before"
    assert downloaded == [(1, 1001), (2, 1993)]


# ---------------------------------------------------------------------------
# Icons that could not be written; "Close Steam & Apply Icons"; Ignore (N2, N25)
# ---------------------------------------------------------------------------

def test_run_fetch_queues_the_icons_when_the_vdf_write_fails(run_env, monkeypatch):
    """N2: the run used to log "Applied 0 icon(s)" and drop the icons."""
    run_env.icon_for = {1}
    monkeypatch.setattr(steam_files, "set_shortcut_icons",
                        lambda m: run_env.calls.append(("write_icons", dict(m))) or None)
    _run(run_env, _games(1))
    assert run_env.calls[-2:] == [("write_icons", {1: "/g/1_icon.png"}),
                                  ("queue_icons", {1: "/g/1_icon.png"})]
    assert any("Couldn't write 1 icon(s) to shortcuts.vdf" in m for m in run_env.logs)
    assert not any(m.startswith("Applied") for m in run_env.logs)


@pytest.fixture
def close_env(monkeypatch):
    rec = types.SimpleNamespace(steps=[], running=[False], flush=0, pending_after=False,
                                stopped=0)
    monkeypatch.setattr(steam_env, "stop_steam", lambda: setattr(rec, "stopped", rec.stopped + 1))
    monkeypatch.setattr(steam_env, "is_steam_running",
                        lambda force=False: rec.running.pop(0) if len(rec.running) > 1
                        else rec.running[0])
    monkeypatch.setattr(steam_files, "apply_pending_icons", lambda: rec.flush)
    monkeypatch.setattr(steam_files, "has_pending_icons", lambda: rec.pending_after)
    return rec


def _close_and_apply(rec):
    return operations.close_steam_and_apply_icons(on_step=rec.steps.append, wait=0.2,
                                                  interval=0.01)


def test_close_steam_and_apply_waits_for_steam_then_writes(close_env):
    close_env.running = [True, True, False]
    close_env.flush = 2
    assert _close_and_apply(close_env) == (operations.ICONS_APPLIED, 2)
    assert close_env.stopped == 1
    assert close_env.steps == ["Closing Steam", "Applying icons"]


def test_close_steam_and_apply_never_writes_while_steam_stays(close_env):
    close_env.running = [True]
    close_env.flush = 5
    assert _close_and_apply(close_env) == (operations.ICONS_STEAM_STAYED, 0)
    assert close_env.steps == ["Closing Steam"]


def test_close_steam_and_apply_reports_a_failed_write(close_env):
    close_env.flush = None
    close_env.pending_after = True
    assert _close_and_apply(close_env) == (operations.ICONS_NOT_WRITTEN, 0)


def test_close_steam_and_apply_counts_an_emptied_queue_as_applied(close_env):
    """The 4 s poll may have flushed the queue first: nothing left is success."""
    close_env.flush = 0
    assert _close_and_apply(close_env) == (operations.ICONS_APPLIED, 0)


def test_ignore_puts_the_game_on_the_skip_list(run_env):
    operations.ignore(7)
    assert run_env.calls == [("skip", 7)]


# ---------------------------------------------------------------------------
# N10 / N6 — one bad game cannot abort the run; an unanswered slot is named
# ---------------------------------------------------------------------------

def test_one_game_that_raises_does_not_abort_the_run(run_env, monkeypatch):
    """N10: the exception used to escape the loop — no seal, and the icons of the
    games already fetched never reached shortcuts.vdf."""
    run_env.icon_for = {2}
    real = artwork.download_all_artwork

    def flaky(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        if app_id == 1:
            raise KeyError("url")
        return real(sgdb_id, app_id, prefs, progress_cb=progress_cb, defer_icon=defer_icon)
    monkeypatch.setattr(artwork, "download_all_artwork", flaky)
    report = _run(run_env, _games(1, 2))
    assert [r["app_id"] for r in report["results"]] == [2]
    assert "seal" in run_env.calls
    assert ("write_icons", {2: "/g/2_icon.png"}) in run_env.calls
    assert any("Something went wrong with Game 1" in m for m in run_env.logs)


def test_the_log_names_a_slot_steamgriddb_did_not_answer(run_env, monkeypatch):
    def partial(sgdb_id, app_id, prefs, progress_cb=None, defer_icon=False):
        art = artwork.FetchedArt()
        art.slots["grids"] = {"label": "Cover", "failed": False, "applied_index": 0,
                              "applied_path": "/g/1p.png"}
        art.slots["icons"] = None
        art.unanswered = ["Icon"]
        return art
    monkeypatch.setattr(artwork, "download_all_artwork", partial)
    _run(run_env, _games(1))
    line = [m for m in run_env.logs if m.startswith("Artwork saved for Game 1")][0]
    assert "SteamGridDB did not answer for Icon" in line
