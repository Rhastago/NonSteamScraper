"""The data folder (R1) and the move of an older version's files into it (R2).

Up to v1.5.2 every app file lived in the home folder as ~/.steamart_<name>; from
v1.5.3 they live in appfiles.DATA_FOLDER (~/.config/NonSteamScraper, or
%APPDATA%\\NonSteamScraper on Windows) and migrate_legacy_files moves the old ones
on the first launch. Every migration test passes its own home and folder under
tmp_path, so nothing here reads or writes the sandbox HOME.

HOME / APPDATA are sandboxed by tests/conftest.py before anything is imported.
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

assert os.path.basename(os.environ["HOME"]).startswith("steamart_test_home_"), (
    "tests/conftest.py must sandbox HOME before the backend is imported")

import appfiles  # noqa: E402

_PATH_NAMES = ("APIKEY_FILE", "PREFS_FILE", "THEME_FILE", "ACCENT_FILE", "ACCOUNT_FILE",
               "FIRSTRUN_FILE", "GEOMETRY_FILE", "SKIP_FILE", "NAMES_FILE", "IDS_FILE",
               "MANAGED_FILE", "CACHE_FOLDER", "BACKUP_FOLDER", "RESTORE_FOLDER",
               "PENDING_ICONS_FILE", "LOCK_FILE")


# ---------------------------------------------------------------------------
# Where the folder is
# ---------------------------------------------------------------------------

def test_the_data_folder_is_dot_config_and_ignores_xdg_config_home(monkeypatch):
    """Q3: always ~/.config/NonSteamScraper. Inside a Flatpak XDG_CONFIG_HOME
    points into that sandbox, so following it would move the folder around."""
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/somewhere/else")
    assert appfiles.data_folder() == os.path.join(os.path.expanduser("~"), ".config",
                                                  "NonSteamScraper")


def test_the_data_folder_is_under_appdata_on_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("APPDATA", os.path.join("C:", "Users", "u", "AppData", "Roaming"))
    assert appfiles.data_folder() == os.path.join("C:", "Users", "u", "AppData", "Roaming",
                                                  "NonSteamScraper")


def test_every_app_path_is_inside_the_data_folder_inside_the_sandbox():
    """The suite can only write where conftest put it: every registry path is in
    DATA_FOLDER, and DATA_FOLDER (HOME- or APPDATA-based) is in the sandbox — on a
    Windows runner too, where it would otherwise be the user's real %APPDATA%."""
    sandbox = os.path.realpath(os.environ["HOME"])
    folder = os.path.realpath(appfiles.DATA_FOLDER)
    assert folder.startswith(sandbox + os.sep), folder
    assert os.path.realpath(os.environ["APPDATA"]).startswith(sandbox + os.sep)
    for name in _PATH_NAMES:
        path = getattr(appfiles, name)
        assert os.path.dirname(path) == appfiles.DATA_FOLDER, f"{name} = {path}"


def test_ensure_data_folder_creates_it(tmp_path, monkeypatch):
    monkeypatch.setattr(appfiles, "DATA_FOLDER", str(tmp_path / "a" / "NonSteamScraper"))
    assert appfiles.ensure_data_folder() is True
    assert (tmp_path / "a" / "NonSteamScraper").is_dir()


# ---------------------------------------------------------------------------
# The move (R2, Q4–Q6)
# ---------------------------------------------------------------------------

@pytest.fixture
def homes(tmp_path):
    home = tmp_path / "home"
    folder = tmp_path / "home" / ".config" / "NonSteamScraper"
    folder.mkdir(parents=True)
    return home, folder


def _old(home, name, text="x"):
    p = home / (".steamart_" + name)
    p.write_text(text, encoding="utf-8")
    return p


def _migrate(home, folder):
    return appfiles.migrate_legacy_files(home=str(home), folder=str(folder))


def test_old_files_and_folders_move_into_the_data_folder(homes):
    home, folder = homes
    for name in ("apikey", "prefs", "theme", "accent", "account", "firstrun", "geometry",
                 "skip", "names", "ids", "managed"):
        _old(home, name, f"{name} data")
    for name in ("cache", "backup", "restore"):
        (home / (".steamart_" + name)).mkdir()
        (home / (".steamart_" + name) / "inside").write_text(name, encoding="utf-8")
    _old(home, "pending_icons_123", '{"5": "/g/5_icon.png"}')
    _old(home, "lock", "")

    summary = _migrate(home, folder)

    assert (folder / "apikey").read_text(encoding="utf-8") == "apikey data"
    assert (folder / "account").read_text(encoding="utf-8") == "account data"
    assert (folder / "restore" / "inside").read_text(encoding="utf-8") == "restore"
    assert (folder / "pending_icons_123").exists()
    assert summary["failed"] == [] and summary["skipped"] is None
    assert len(summary["moved"]) == 15
    left = [n for n in os.listdir(home) if n.startswith(".steamart")]
    assert left == [], f"the home folder still holds {left}"
    assert summary["lock_removed"] is True, "Q5: the empty, unheld old lock goes"


def test_a_second_run_moves_nothing_and_changes_nothing(homes):
    home, folder = homes
    _old(home, "apikey", "k")
    _migrate(home, folder)
    before = sorted(os.listdir(folder))
    summary = _migrate(home, folder)
    assert sorted(os.listdir(folder)) == before
    assert not any(summary[k] for k in ("moved", "copied", "merged", "kept", "failed"))


def test_when_both_exist_the_new_copy_wins_and_the_old_is_left_alone(homes):
    home, folder = homes
    _old(home, "apikey", "old key")
    (folder / "apikey").write_text("new key", encoding="utf-8")
    summary = _migrate(home, folder)
    assert (folder / "apikey").read_text(encoding="utf-8") == "new key"
    assert (home / ".steamart_apikey").read_text(encoding="utf-8") == "old key"
    assert summary["kept"] == ["apikey"]


def test_both_managed_registries_are_merged(homes):
    """Q6: the union of the two, so Clear All Artwork still knows every file the
    app wrote, whichever version wrote it."""
    home, folder = homes
    _old(home, "managed", "/g/1p.png\n/g/2p.png\n")
    (folder / "managed").write_text("/g/2p.png\n/g/3p.png\n", encoding="utf-8")
    summary = _migrate(home, folder)
    assert (folder / "managed").read_text(encoding="utf-8").split() == \
        ["/g/2p.png", "/g/3p.png", "/g/1p.png"]
    assert not (home / ".steamart_managed").exists()
    assert summary["merged"] == ["managed"]


def test_both_backup_folders_merge_without_overwriting(homes):
    home, folder = homes
    old = home / ".steamart_backup"
    old.mkdir()
    (old / "shortcuts.vdf.1.bak").write_text("old 1", encoding="utf-8")
    (old / "shortcuts.vdf.2.bak").write_text("old 2", encoding="utf-8")
    (folder / "backup").mkdir()
    (folder / "backup" / "shortcuts.vdf.1.bak").write_text("new 1", encoding="utf-8")
    _migrate(home, folder)
    assert (folder / "backup" / "shortcuts.vdf.1.bak").read_text(encoding="utf-8") == "new 1"
    assert (folder / "backup" / "shortcuts.vdf.2.bak").read_text(encoding="utf-8") == "old 2"
    assert (old / "shortcuts.vdf.1.bak").read_text(encoding="utf-8") == "old 1", \
        "a file the new folder already has stays where it was"


def test_a_failed_rename_copies_and_keeps_the_original(homes, monkeypatch):
    """Q4: another drive (or a file held open on Windows) makes the rename fail;
    the file is copied and the original is kept — nothing is deleted."""
    home, folder = homes
    _old(home, "prefs", "p")
    (home / ".steamart_restore").mkdir()
    (home / ".steamart_restore" / "manifest.json").write_text("{}", encoding="utf-8")
    real_replace = os.replace

    def cross_drive(src, dst):
        if ".steamart_" in str(src):
            raise OSError(18, "Invalid cross-device link")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", cross_drive)

    summary = _migrate(home, folder)

    assert sorted(summary["copied"]) == ["prefs", "restore"]
    assert (folder / "prefs").read_text(encoding="utf-8") == "p"
    assert (folder / "restore" / "manifest.json").exists()
    assert (home / ".steamart_prefs").exists() and (home / ".steamart_restore").exists()


def test_a_copy_that_fails_leaves_no_partial_file_and_is_retried(homes, monkeypatch):
    home, folder = homes
    _old(home, "skip", "1\n2\n")
    real_replace = os.replace

    def refuse(src, dst):
        if ".steamart_" in str(src):
            raise OSError(18, "Invalid cross-device link")
        return real_replace(src, dst)

    def broken_copy(src, dst, *a, **k):
        with open(dst, "w", encoding="utf-8") as f:
            f.write("1\n")           # half of it, then the disk fills up
        raise OSError(28, "No space left on device")

    def no_cleanup(path):
        raise OSError(5, "I/O error")   # like a crash: the partial file stays
    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr(appfiles.shutil, "copy2", broken_copy)
    monkeypatch.setattr(os, "remove", no_cleanup)

    summary = _migrate(home, folder)

    assert summary["failed"] == ["skip"]
    assert not (folder / "skip").exists(), "a partial copy would be taken for the real file"
    assert (home / ".steamart_skip").read_text(encoding="utf-8") == "1\n2\n"

    monkeypatch.undo()
    assert _migrate(home, folder)["moved"] == ["skip"]


def test_only_the_named_files_and_the_pending_icon_family_move(homes):
    """Never a glob of ~/.steamart_*: a crash-left temp file or an unknown name
    stays where it is."""
    home, folder = homes
    _old(home, "pending_icons", "{}")
    _old(home, "pending_icons_42", "{}")
    _old(home, "pending_icons_42.ab12cd34.tmp", "{")
    _old(home, "something_else", "?")
    _migrate(home, folder)
    assert sorted(os.listdir(folder)) == ["pending_icons", "pending_icons_42"]
    assert (home / ".steamart_pending_icons_42.ab12cd34.tmp").exists()
    assert (home / ".steamart_something_else").exists()


_OLD_COPY = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
import appfiles
appfiles.LOCK_FILE = sys.argv[2]
print("held" if appfiles.acquire_instance_lock(wait=0) else "refused", flush=True)
time.sleep(30)
"""


def test_nothing_moves_while_an_older_copy_holds_the_old_lock(homes):
    """An older copy that is still running keeps writing ~/.steamart_*: moving
    the files under it would split the user's data in two."""
    home, folder = homes
    _old(home, "apikey", "k")
    lock = _old(home, "lock", "")
    p = subprocess.Popen([sys.executable, "-c", _OLD_COPY, ROOT, str(lock)],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        summary = _migrate(home, folder)
    finally:
        p.kill(); p.wait(); p.stdout.close()
    assert summary["skipped"]
    assert (home / ".steamart_apikey").exists() and os.listdir(folder) == []
    assert lock.exists() and summary["lock_removed"] is False


def test_a_non_empty_old_lock_file_is_not_removed(homes):
    """Q5 covers the empty lock file the app made, nothing else."""
    home, folder = homes
    lock = _old(home, "lock", "not ours")
    assert _migrate(home, folder)["lock_removed"] is False
    assert lock.exists()


def test_the_original_a_copy_left_behind_is_not_reported_again(homes, monkeypatch):
    """Q4 keeps the original after a copy; later launches must not warn about it
    on every start (it is identical to the new copy)."""
    home, folder = homes
    _old(home, "prefs", "p")
    (home / ".steamart_cache").mkdir()
    (home / ".steamart_cache" / "1p_0.png").write_text("img", encoding="utf-8")
    real_replace = os.replace

    def cross_drive(src, dst):
        if ".steamart_" in str(src):
            raise OSError(18, "Invalid cross-device link")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", cross_drive)
    assert sorted(_migrate(home, folder)["copied"]) == ["cache", "prefs"]

    again = _migrate(home, folder)
    assert sorted(again["same"]) == ["cache", "prefs"] and again["kept"] == []
    (home / ".steamart_prefs").write_text("changed", encoding="utf-8")
    assert _migrate(home, folder)["kept"] == ["prefs"], "a real difference is still reported"
