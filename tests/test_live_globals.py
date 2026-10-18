"""G-LIVE: an account switch reaches every writer.

steam_env owns the active account's paths and set_active_user() rebinds them.
Every reader must look them up live (steam_env.GRID_FOLDER at call time); a
by-value copy anywhere — a facade re-export, a `from steam_env import
GRID_FOLDER` — keeps writing into the OLD account's folders after a switch (the
CLAUDE.md non-negotiable). These tests switch from account A to account B on a
real two-account Steam folder in tmp_path and check where the writes land.

HOME is sandboxed by tests/conftest.py before anything is imported.
"""

import json
import os
import subprocess
import sys

import pytest
import vdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

assert os.path.basename(os.environ["HOME"]).startswith("steamart_test_home_"), (
    "tests/conftest.py must sandbox HOME before find_games is imported")

import find_games as fg  # noqa: E402
import restore
import steam_files
import sgdb
import appfiles  # noqa: E402
import steam_env  # noqa: E402

APP_ID = 777


def _shortcuts(path):
    with open(path, "wb") as f:
        f.write(vdf.binary_dumps({"shortcuts": {"0": {"appid": APP_ID, "AppName": "G",
                                                       "icon": ""}}}))


def _icon_in(path):
    with open(path, "rb") as f:
        return vdf.binary_loads(f.read())["shortcuts"]["0"]["icon"]


@pytest.fixture
def two_accounts(tmp_path, monkeypatch):
    """Steam userdata with accounts A (111) and B (222), each with a
    shortcuts.vdf; every app file patched into tmp_path. Starts on A."""
    userdata = tmp_path / "userdata"
    for uid in ("111", "222"):
        (userdata / uid / "config").mkdir(parents=True)
        _shortcuts(userdata / uid / "config" / "shortcuts.vdf")
    monkeypatch.setattr(steam_env, "get_steam_path", lambda: str(userdata))
    for name in ("ACCOUNT_FILE", "RESTORE_FOLDER", "MANAGED_FILE", "BACKUP_FOLDER",
                 "CACHE_FOLDER", "PENDING_ICONS_FILE"):
        monkeypatch.setattr(appfiles, name, str(tmp_path / name.lower()))
    for name in ("STEAM_USERDATA", "STEAM_USER_ID", "SHORTCUTS_PATH", "GRID_FOLDER",
                 "STEAM_NOT_FOUND"):
        monkeypatch.setattr(steam_env, name, getattr(steam_env, name))   # restored after
    monkeypatch.setattr(steam_env, "is_steam_running", lambda *a, **k: False)
    assert steam_env.set_active_user("111")
    return userdata


def _paths(userdata, uid):
    return (str(userdata / uid / "config" / "grid"),
            str(userdata / uid / "config" / "shortcuts.vdf"))


def test_switch_rebinds_the_owning_module_and_leaves_no_copy_behind(two_accounts):
    grid_b, vdf_b = _paths(two_accounts, "222")
    assert steam_env.set_active_user("222")
    assert steam_env.GRID_FOLDER == grid_b
    assert steam_env.SHORTCUTS_PATH == vdf_b
    assert steam_env.get_active_user() == "222"
    assert open(appfiles.ACCOUNT_FILE, encoding="utf-8").read() == "222"
    for name in ("GRID_FOLDER", "SHORTCUTS_PATH", "STEAM_USER_ID"):
        assert not hasattr(fg, name), (
            f"find_games must not hold a copy of {name}: a copy keeps the old account")


def test_a_fetch_download_after_the_switch_lands_in_the_new_account(two_accounts, monkeypatch):
    grid_a, _ = _paths(two_accounts, "111")
    grid_b, _ = _paths(two_accounts, "222")
    steam_env.set_active_user("222")
    monkeypatch.setattr(sgdb, "get_artwork", lambda sgdb_id, art_type, prefs=None, dimensions=None:
                        [{"url": f"https://x/{art_type}.png", "mime": "image/png"}]
                        if art_type == "grids" else [])
    monkeypatch.setattr(sgdb, "get_steam_platform_data", lambda sgdb_id: None)

    def fake_download(url, save_path, register=True):
        with open(save_path, "wb") as f:
            f.write(b"png")
        return True
    monkeypatch.setattr(fg, "download_artwork", fake_download)

    fg.download_all_artwork(1, APP_ID, prefs={}, defer_icon=True)

    assert sorted(os.listdir(grid_b)) == [f"{APP_ID}.png", f"{APP_ID}p.png"]
    assert os.listdir(grid_a) == [], "nothing may land in the old account's grid"


def test_an_icon_write_after_the_switch_lands_in_the_new_account(two_accounts):
    _, vdf_a = _paths(two_accounts, "111")
    _, vdf_b = _paths(two_accounts, "222")
    steam_env.set_active_user("222")

    assert steam_files.set_shortcut_icon(APP_ID, "/icons/b.png") is True

    assert _icon_in(vdf_b) == "/icons/b.png"
    assert _icon_in(vdf_a) == "", "the old account's shortcuts.vdf must be untouched"
    assert os.path.exists(os.path.join(appfiles.BACKUP_FOLDER, "shortcuts.vdf.222.bak"))


def test_the_restore_point_refuses_the_old_accounts_manifest(two_accounts):
    grid_a, _ = _paths(two_accounts, "111")
    with open(os.path.join(grid_a, f"{APP_ID}p.png"), "wb") as f:
        f.write(b"a's art")
    restore.snapshot_for_restore(APP_ID)
    assert restore.has_restore_point() is True

    steam_env.set_active_user("222")

    assert restore.has_restore_point() is False
    assert restore.undo_restore_point()["games"] == 0
    with open(os.path.join(appfiles.RESTORE_FOLDER, "manifest.json"), encoding="utf-8") as f:
        assert json.load(f)["grid_folder"] == grid_a, "A's restore point stays A's"


def test_init_picks_the_saved_account_and_creates_its_grid(two_accounts):
    with open(appfiles.ACCOUNT_FILE, "w", encoding="utf-8") as f:
        f.write("222")
    steam_env.init()
    grid_b, vdf_b = _paths(two_accounts, "222")
    assert steam_env.STEAM_NOT_FOUND is False
    assert (steam_env.STEAM_USER_ID, steam_env.GRID_FOLDER, steam_env.SHORTCUTS_PATH) == \
        ("222", grid_b, vdf_b)
    assert os.path.isdir(grid_b)


def test_importing_the_backend_touches_nothing_under_home(tmp_path):
    """A4: importing find_games used to detect Steam, read the account file and
    create the grid folder. Now only steam_env.init() does that."""
    home = tmp_path / "home"
    (home / ".steam" / "steam" / "userdata" / "111" / "config").mkdir(parents=True)
    _shortcuts(home / ".steam" / "steam" / "userdata" / "111" / "config" / "shortcuts.vdf")
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import find_games, steam_env, appfiles\n"
            "assert steam_env.STEAM_NOT_FOUND and steam_env.GRID_FOLDER == ''\n" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    after = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert after == before, f"import created {sorted(set(after) - set(before))}"
