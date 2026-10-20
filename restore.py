"""The Undo restore point (appfiles.RESTORE_FOLDER): byte copies of a game's art
taken before a fetch or a Re-fetch changes it, the ordered rules that decide
which copy wins, sealing a run, and putting it all back. Also clear_game_art,
the ONE deletion behind Re-fetch and Search → Select. tkinter-free; every
function here never raises.

A restore point describes exactly one account: it records the grid folder it
was taken from and refuses any other (steam_env.GRID_FOLDER, read live)."""

import json
import os
import re
import shutil
import time
from appcommon import debug as _debug
import appfiles
import steam_env
import steam_files

# RESTORE_FOLDER holds ONE restore point: manifest.json plus <id>/<file>
# byte copies (mtime preserved via copy2) taken before a fetch or Re-fetch
# destroys art, so Undo can put back what was there. The manifest is written
# atomically (temp file + os.replace), the same rule as the shortcuts.vdf writer
# and the download path.


def _restore_manifest_path():
    """Absolute path of the restore-point manifest. Lives inside RESTORE_FOLDER
    so monkeypatching RESTORE_FOLDER redirects the whole store (tests)."""
    return os.path.join(appfiles.RESTORE_FOLDER, "manifest.json")

def _load_restore_manifest():
    """Load the restore manifest as a dict. A missing or corrupt manifest loads
    as an empty one ({"version", "grid_folder", "entries"}); never raises."""
    try:
        with open(_restore_manifest_path(), encoding="utf-8") as f:
            m = json.load(f)
        if not isinstance(m, dict) or not isinstance(m.get("entries"), dict):
            return {"version": 1, "grid_folder": "", "entries": {}}
        return m
    except Exception:
        _debug("_load_restore_manifest")
        return {"version": 1, "grid_folder": "", "entries": {}}

def _save_restore_manifest(m):
    """Atomically write the restore manifest through appfiles.write_text_atomic (a
    unique temp file, fsync, os.replace — N15: a fixed temp name and no fsync before)
    — a crash mid-write can never leave a corrupt manifest that pretends copies exist
    which do not."""
    os.makedirs(appfiles.RESTORE_FOLDER, exist_ok=True)
    appfiles.write_text_atomic(_restore_manifest_path(), json.dumps(m, indent=2))

def _files_matching_id(fnames, unsigned_id):
    """The filenames whose LEADING DIGIT RUN equals str(unsigned_id). NEVER a
    bare str(uid) prefix: uid 12345 would match 123456p.png, a DIFFERENT game
    (steam_files.get_non_steam_games documents the same false positive)."""
    uid = str(unsigned_id)
    matched = []
    for f in fnames:
        m = re.match(r"(\d+)", f)
        if m and m.group(1) == uid:
            matched.append(f)
    return matched

def snapshot_for_restore(unsigned_id, existing_files=None, pending=False):
    """Record a restore point for one game before its art is about to change.
    The rules are ORDERED and the order is the correctness:
      1. Account guard first: if the manifest's grid_folder is set and differs
         from the live GRID_FOLDER (account switch), wipe the whole store and
         start empty — a restore point describes exactly one account.
      2. An existing UNSEALED entry wins: return unchanged. This stops a second
         consecutive Re-fetch from snapshotting the now-empty folder over good
         copies, and a fetch's own snapshot never overwrites the Re-fetch
         snapshot that preceded it.
      3. An existing SEALED entry is superseded: delete its copies from disk,
         drop it, and snapshot the current art.
      4. Always record an entry, even with no art and no icon — Undo needs it
         to delete what the fetch adds and return the game to "needs art".
    existing_files: optional pre-listed GRID_FOLDER snapshot — download_all_artwork
    shares ONE listdir across this game's slots; standalone callers leave None.
    Never raises."""
    try:
        m = _load_restore_manifest()
        if m.get("grid_folder") and m.get("grid_folder") != steam_env.GRID_FOLDER:
            shutil.rmtree(appfiles.RESTORE_FOLDER, ignore_errors=True)
            m = {"version": 1, "grid_folder": "", "entries": {}}
        eid = str(unsigned_id)
        entry = m.get("entries", {}).get(eid)
        if entry is not None and not entry.get("sealed"):
            return  # unsealed entry wins — never re-snapshot over it
        if entry is not None:
            # sealed entry is superseded: its copies belong to an accepted run
            shutil.rmtree(os.path.join(appfiles.RESTORE_FOLDER, eid), ignore_errors=True)
            del m["entries"][eid]
        managed = appfiles.load_managed_files()
        if existing_files is None:
            try:
                existing_files = os.listdir(steam_env.GRID_FOLDER)
            except OSError:
                existing_files = []
        entry_dir = os.path.join(appfiles.RESTORE_FOLDER, eid)
        os.makedirs(entry_dir, exist_ok=True)
        files = []
        managed_subset = []
        for fname in _files_matching_id(existing_files, unsigned_id):
            src = os.path.join(steam_env.GRID_FOLDER, fname)
            try:
                shutil.copy2(src, os.path.join(entry_dir, fname))
            except OSError:
                _debug("snapshot_for_restore copy")
                continue
            files.append(fname)
            if src in managed:
                managed_subset.append(fname)
        m["grid_folder"] = steam_env.GRID_FOLDER
        m["entries"][eid] = {
            "files": files,
            "managed": managed_subset,
            "icon": steam_files._icon_from_shortcuts_vdf(unsigned_id),
            "pending": bool(pending),
            "sealed": False,
            "created": time.time(),
        }
        _save_restore_manifest(m)
    except Exception:
        _debug("snapshot_for_restore")

def clear_game_art(unsigned_id):
    """Clear ONE game's art from the live GRID_FOLDER so it is fetched again: the
    one deletion behind Re-fetch and Search → Select. Snapshots first (pending=True:
    outside any fetch run), so Undo can put the art back, then deletes only the
    files whose leading digit run equals this id — NEVER glob(f"{uid}*"), which
    would also delete game 123456's art when clearing 12345. Returns the number
    of files removed. Never raises."""
    snapshot_for_restore(unsigned_id, pending=True)
    removed = 0
    try:
        fnames = os.listdir(steam_env.GRID_FOLDER)
    except OSError:
        return 0
    for fname in _files_matching_id(fnames, unsigned_id):
        try:
            os.remove(os.path.join(steam_env.GRID_FOLDER, fname))
            removed += 1
        except OSError:
            _debug("clear_game_art remove")
    return removed

def begin_restore_run():
    """Start of a fetch run: drop every SEALED entry (deleting its copies from
    disk as well as its manifest row, so the store cannot leak like the old
    backup-folder pile) and clear `pending` on the rest, adopting the
    Re-fetches performed since the last run into this one. Never raises."""
    try:
        m = _load_restore_manifest()
        if not m.get("entries"):
            return
        for eid in [e for e, entry in m["entries"].items() if entry.get("sealed")]:
            shutil.rmtree(os.path.join(appfiles.RESTORE_FOLDER, eid), ignore_errors=True)
            del m["entries"][eid]
        for entry in m["entries"].values():
            entry["pending"] = False
        _save_restore_manifest(m)
    except Exception:
        _debug("begin_restore_run")

def seal_restore_point():
    """End of a fetch run: mark every entry sealed — the operator has accepted
    this run's art. Never raises."""
    try:
        m = _load_restore_manifest()
        if not m.get("entries"):
            return
        for entry in m["entries"].values():
            entry["sealed"] = True
        _save_restore_manifest(m)
    except Exception:
        _debug("seal_restore_point")

def has_restore_point():
    """True when the manifest exists with at least one entry AND its grid_folder
    matches the live GRID_FOLDER. The account check lives here (not only in the
    snapshot path) so switching accounts disables Undo instead of pointing it at
    the wrong folder. This on-disk state, not memory, drives the Undo button.
    Never raises."""
    try:
        m = _load_restore_manifest()
        return bool(m.get("entries")) and m.get("grid_folder") == steam_env.GRID_FOLDER
    except Exception:
        _debug("has_restore_point")
        return False

def restore_point_game_count():
    """How many games the restore point holds (Undo's confirmation names it).
    Never raises."""
    return len(_load_restore_manifest().get("entries", {}))

def _copy_back(src, dst):
    """Put one saved file back: a copy next to dst, then os.replace onto it, so a
    copy that fails half-way never leaves a truncated image in the grid folder.
    Raises OSError."""
    tmp = dst + ".part"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def _undo_entry(eid, entry, managed):
    """Undo one game. FIRST copy every saved file back (replacing a same-named
    current file); only when all of them are back delete the app's current files
    for this id that the snapshot did not have (never a file this app did not write,
    never a neighbouring game's). A failed copy stops there: the game keeps its
    current art and the entry stays for a retry (N9: deleting first left a game with
    neither when a copy then failed). Then the registry: a restored file the app had
    written is registered again, one that was the user's own is taken OFF it (N1),
    and deleted files leave it. Returns (ok, files copied back, files removed)."""
    names = entry.get("files", [])
    copied = 0
    for fname in names:
        try:
            _copy_back(os.path.join(appfiles.RESTORE_FOLDER, eid, fname),
                       os.path.join(steam_env.GRID_FOLDER, fname))
            copied += 1
        except OSError:
            _debug("undo_restore_point copy")
            return False, copied, 0
    gone = []
    try:
        for fname in _files_matching_id(os.listdir(steam_env.GRID_FOLDER), eid):
            cur = os.path.join(steam_env.GRID_FOLDER, fname)
            if fname not in names and cur in managed:
                try:
                    os.remove(cur)
                    gone.append(cur)
                except OSError:
                    _debug("undo_restore_point remove")
    except OSError:
        _debug("undo_restore_point listdir")
    own = set(entry.get("managed", []))
    try:
        for fname in names:
            if fname in own:
                appfiles.register_managed_file(os.path.join(steam_env.GRID_FOLDER, fname))
        appfiles.unregister_managed_files(
            [os.path.join(steam_env.GRID_FOLDER, f) for f in names if f not in own] + gone)
    except OSError:
        _debug("undo_restore_point registry")
    return True, copied, len(gone)

def undo_restore_point():
    """Restore the art recorded in the restore point. If has_restore_point() is
    false — including the account-mismatch case — do nothing and return a zero
    summary; never restore across accounts. Each entry is undone by _undo_entry;
    {unsigned_id: icon} is collected for every restored entry whose recorded icon is
    not null and written once through steam_files.write_or_queue_icons (queued while
    Steam runs or when the write fails). Restored entries leave the store; an entry
    whose copy failed stays, with its copies, so Undo can be pressed again — the
    store is wiped only when nothing is left.
    Returns {games, files, removed, queued, failed, icon_write_failed}: games and
    files restored, files removed, whether the icons were queued, the number of
    games kept for a retry, and whether the icon write failed. Never raises."""
    zero = {"games": 0, "files": 0, "removed": 0, "queued": False, "failed": 0,
            "icon_write_failed": False}
    try:
        if not has_restore_point():
            return zero
        m = _load_restore_manifest()
        managed = appfiles.load_managed_files()
        icons = {}
        restored = removed = games = 0
        kept = {}
        for eid, entry in m.get("entries", {}).items():
            ok, copied, gone = _undo_entry(eid, entry, managed)
            restored += copied
            removed += gone
            if not ok:
                kept[eid] = entry
                continue
            games += 1
            # the icon ("" restores "no icon"; None = no shortcut)
            icon = entry.get("icon")
            if icon is not None:
                try:
                    icons[int(eid)] = icon
                except (ValueError, TypeError):
                    _debug("undo_restore_point icon key")
        written, queued = steam_files.write_or_queue_icons(icons)
        if kept:
            for eid in m["entries"]:
                if eid not in kept:
                    shutil.rmtree(os.path.join(appfiles.RESTORE_FOLDER, eid), ignore_errors=True)
            m["entries"] = kept
            _save_restore_manifest(m)
        else:
            clear_restore_point()
        return {"games": games, "files": restored, "removed": removed, "queued": queued,
                "failed": len(kept), "icon_write_failed": written is None}
    except Exception:
        _debug("undo_restore_point")
        return zero

def clear_restore_point():
    """Wipe the whole restore store (manifest + copies). Never raises."""
    shutil.rmtree(appfiles.RESTORE_FOLDER, ignore_errors=True)

def restore_point_size():
    """MB occupied by the restore store, rounded to 2 dp (mirrors
    get_cache_size). The store nests <id>/<file>, so this walks recursively."""
    if not os.path.exists(appfiles.RESTORE_FOLDER):
        return 0.0
    total = 0
    try:
        for root, _dirs, files in os.walk(appfiles.RESTORE_FOLDER):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        return 0.0
    return round(total / (1024 * 1024), 2)
