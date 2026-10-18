import os, re, sys, platform, shutil, subprocess, time, json, threading, glob, tempfile, vdf, requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote as _url_quote
from datetime import datetime, timedelta, timezone
import appfiles

from appcommon import DEBUG, debug as _debug
import steam_env
import sgdb


def clear_managed_artwork():
    managed = appfiles.load_managed_files(); deleted = 0
    for p in managed:
        if os.path.exists(p): os.remove(p); deleted += 1
    if os.path.exists(appfiles.MANAGED_FILE): os.remove(appfiles.MANAGED_FILE)
    # The restore point snapshots the very art being cleared; leaving it behind
    # would let a later Undo silently re-copy and re-register everything the
    # operator just removed ("Clear All Artwork" promises to remove it all).
    clear_restore_point()
    return deleted


# --- Restore point (SPEC.md S3) --------------------------------------------------
# ~/.steamart_restore/ holds ONE restore point: manifest.json plus <id>/<file>
# byte copies (mtime preserved via copy2) taken before a fetch or Re-fetch
# destroys art, so Undo can put back what was there. The manifest is written
# atomically (temp file + os.replace), the same rule as _write_shortcuts_atomic
# and the download path. Every function here never raises (module contract).

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
    """Atomically write the restore manifest: temp file inside RESTORE_FOLDER,
    then os.replace() onto position — a crash mid-write can never leave a
    corrupt manifest that pretends copies exist which do not."""
    os.makedirs(appfiles.RESTORE_FOLDER, exist_ok=True)
    tmp = os.path.join(appfiles.RESTORE_FOLDER, "manifest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)
    os.replace(tmp, _restore_manifest_path())

def _files_matching_id(fnames, unsigned_id):
    """The filenames whose LEADING DIGIT RUN equals str(unsigned_id). NEVER a
    bare str(uid) prefix: uid 12345 would match 123456p.png, a DIFFERENT game
    (find_games.py:549-557 documents the same false positive)."""
    uid = str(unsigned_id)
    matched = []
    for f in fnames:
        m = re.match(r"(\d+)", f)
        if m and m.group(1) == uid:
            matched.append(f)
    return matched

def _icon_from_shortcuts_vdf(unsigned_id):
    """Read the shortcut's current `icon` value from shortcuts.vdf: "" when the
    shortcut has no icon, None (JSON null) when no shortcut matched. Never
    raises."""
    if not os.path.exists(steam_env.SHORTCUTS_PATH):
        return None
    try:
        with open(steam_env.SHORTCUTS_PATH, "rb") as f:
            data = vdf.binary_loads(f.read())
        for _key, game in data.get("shortcuts", {}).items():
            aid = game.get("appid")
            if aid is None:
                continue
            if (aid & 0xFFFFFFFF) == unsigned_id:
                return game.get("icon") or ""
    except Exception:
        _debug("_icon_from_shortcuts_vdf")
    return None

def snapshot_for_restore(unsigned_id, existing_files=None, pending=False):
    """Record a restore point for one game before its art is about to change.
    The rules are ORDERED and the order is the correctness (SPEC.md S3):
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
            "icon": _icon_from_shortcuts_vdf(unsigned_id),
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
    ~/.steamart_backup pile) and clear `pending` on the rest, adopting the
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

def undo_restore_point():
    """Restore the art recorded in the restore point. If has_restore_point() is
    false — including the account-mismatch case — do nothing and return a zero
    summary; never restore across accounts. Per entry: delete the current files
    in GRID_FOLDER whose leading digit run equals this id AND that are in the
    managed registry (never a file this app did not write, never a neighbouring
    game's), copy the snapshot's files back and re-register the managed subset,
    and collect {unsigned_id: icon} for every entry whose recorded icon is not
    null. Then, once: queue the icons via save_pending_icons() when Steam is
    running, else write them with set_shortcut_icons(). Finally wipe the store.
    Returns {games, files, removed, queued}. Never raises."""
    zero = {"games": 0, "files": 0, "removed": 0, "queued": False}
    try:
        if not has_restore_point():
            return zero
        m = _load_restore_manifest()
        managed = appfiles.load_managed_files()
        icons = {}
        restored = 0
        removed = 0
        for eid, entry in m.get("entries", {}).items():
            # 1. delete current files in the managed registry matching this id
            try:
                for fname in _files_matching_id(os.listdir(steam_env.GRID_FOLDER), eid):
                    cur = os.path.join(steam_env.GRID_FOLDER, fname)
                    if cur in managed:
                        try:
                            os.remove(cur)
                            removed += 1
                        except OSError:
                            pass
            except OSError:
                _debug("undo_restore_point listdir")
            # 2. copy the snapshot's files back; re-register the managed subset
            for fname in entry.get("files", []):
                dst = os.path.join(steam_env.GRID_FOLDER, fname)
                try:
                    shutil.copy2(os.path.join(appfiles.RESTORE_FOLDER, eid, fname), dst)
                    restored += 1
                except OSError:
                    _debug("undo_restore_point copy")
                    continue
                if fname in entry.get("managed", []):
                    try:
                        appfiles.register_managed_file(dst)
                    except OSError:
                        _debug("undo_restore_point register")
            # 3. collect the icon ("" restores "no icon"; None = no shortcut)
            icon = entry.get("icon")
            if icon is not None:
                try:
                    icons[int(eid)] = icon
                except (ValueError, TypeError):
                    _debug("undo_restore_point icon key")
        queued = False
        if icons:
            if steam_env.is_steam_running():
                save_pending_icons(icons)
                queued = True
            else:
                set_shortcut_icons(icons)
        clear_restore_point()
        return {"games": len(m.get("entries", {})), "files": restored,
                "removed": removed, "queued": queued}
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

def _is_shortcuts_backup(fname):
    """True for a shortcuts.vdf backup in BACKUP_FOLDER: the legacy
    shortcuts.vdf.bak (one copy, taken for whichever account wrote first before
    v1.5.2) or a per-account shortcuts.vdf.<userid>.bak. Neither is ever deleted."""
    return fname.startswith("shortcuts.vdf.") and fname.endswith(".bak")

def _shortcuts_backup_path():
    """The ACTIVE account's shortcuts.vdf backup. Per account (B10): one shared
    file meant only the first account ever written got a backup. Reads the live
    STEAM_USER_ID, so an account switch is honored."""
    return os.path.join(appfiles.BACKUP_FOLDER, f"shortcuts.vdf.{steam_env.STEAM_USER_ID}.bak")

def legacy_backup_size():
    """MB of the art copies still sitting in ~/.steamart_backup from every past
    fetch (backup_artwork is gone; this reports what it left behind), rounded to
    2 dp like get_cache_size. The shortcuts.vdf backups are EXCLUDED — they are
    never deleted (the only pre-app shortcut-list copies), so the Settings clear
    button could never zero them out. Never raises."""
    if not os.path.exists(appfiles.BACKUP_FOLDER):
        return 0.0
    try:
        total = 0
        for f in os.listdir(appfiles.BACKUP_FOLDER):
            if _is_shortcuts_backup(f): continue  # never deleted; not clearable
            p = os.path.join(appfiles.BACKUP_FOLDER, f)
            if os.path.isfile(p):
                total += os.path.getsize(p)
    except OSError:
        return 0.0
    return round(total / (1024 * 1024), 2)

def clear_backup():
    if not os.path.exists(appfiles.BACKUP_FOLDER): return
    for f in os.listdir(appfiles.BACKUP_FOLDER):
        if _is_shortcuts_backup(f): continue
        p = os.path.join(appfiles.BACKUP_FOLDER, f)
        if os.path.isfile(p): os.remove(p)


def get_non_steam_games():
    if not os.path.exists(steam_env.SHORTCUTS_PATH): return []
    with open(steam_env.SHORTCUTS_PATH, "rb") as f:
        data = vdf.binary_loads(f.read())
    shortcuts = data.get("shortcuts", {})
    skip_list = appfiles.load_skip_list(); seen = set(); games = []

    # --- Task #3: single grid-folder scan with boundary-correct matching -------
    # Build a set of uids that have at least one art file by listing GRID_FOLDER
    # exactly once (O(files)) instead of once per game (O(games × files)).
    #
    # Boundary fix: `startswith(str(uid))` has a prefix false-positive — uid
    # 12345 would match filename "123456p.png" (a DIFFERENT game). Instead, we
    # extract each filename's LEADING DIGIT RUN via regex so "123456p.png"
    # contributes uid "123456", which will never equal "12345".
    #
    # Grid filenames follow these templates:
    #   {uid}p.png   {uid}.png   {uid}_hero.png   {uid}_logo.png   {uid}_icon.png
    # The uid is always the run of digits before the first 'p', '.', or '_'.
    # re.match(r"(\d+)", filename) extracts exactly that leading digit run.
    #
    # If GRID_FOLDER doesn't exist or listdir fails, uids_with_art stays empty
    # so has_art falls through to the skip-list check without crashing.
    uids_with_art = set()
    try:
        for fname in os.listdir(steam_env.GRID_FOLDER):
            m = re.match(r"(\d+)", fname)
            if m:
                uids_with_art.add(m.group(1))
    except OSError:
        pass  # GRID_FOLDER missing or unreadable — treat as no art files
    # ---------------------------------------------------------------------------

    for _key, game in shortcuts.items():
        app_id = game.get("appid")
        if app_id is None: continue
        name = game.get("AppName") or game.get("appname") or "unknown"
        uid = app_id & 0xFFFFFFFF
        if uid in seen: continue
        seen.add(uid)
        in_skip = str(uid) in skip_list
        games.append({"name": name, "app_id": uid, "has_art": (str(uid) in uids_with_art) or in_skip, "skipped": in_skip})
    return games

# ONE writer at a time for shortcuts.vdf (B4). The fetch worker, the 4 s pending-icon
# poll, the results window's apply flows and Undo all write it, from two threads; each
# does read -> change -> write, so without this lock two of them could read the same
# original and the second write would drop the first one's icons. Held by
# set_shortcut_icons around the whole read-modify-write — the only writer.
_SHORTCUTS_LOCK = threading.Lock()

def _write_shortcuts_atomic(data):
    """Serialize `data` and atomically replace shortcuts.vdf: write a UNIQUE temp file
    (tempfile.mkstemp) in the SAME directory, fsync it, then os.replace() onto
    SHORTCUTS_PATH. os.replace is atomic within one filesystem, so a crash/clobber
    mid-write can never leave a truncated vdf; the fsync makes sure the bytes are on
    disk before the rename (a power loss right after a write on ext4 can otherwise
    leave an empty file). The caller holds _SHORTCUTS_LOCK."""
    payload = vdf.binary_dumps(data)
    folder = os.path.dirname(steam_env.SHORTCUTS_PATH)
    fd, tmp = tempfile.mkstemp(prefix="shortcuts.vdf.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates the file owner-only; keep the original file's mode.
        try:
            shutil.copymode(steam_env.SHORTCUTS_PATH, tmp)
        except OSError:
            _debug("_write_shortcuts_atomic copymode")
        os.replace(tmp, steam_env.SHORTCUTS_PATH)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def set_shortcut_icons(mapping):
    """Batch-write icons into shortcuts.vdf in ONE read+parse+write instead of one full
    rewrite per game. `mapping` is {unsigned_id: icon_path}. Every shortcut whose
    (appid & 0xFFFFFFFF) is a key gets game["icon"] set (all shortcuts sharing an appid,
    like the single-write path). Backs up on first write; writes atomically. Returns the
    count of shortcuts changed. Never raises (matches set_shortcut_icon's style).
    The backup is per account (shortcuts.vdf.<userid>.bak), taken on that account's
    first write; the legacy shortcuts.vdf.bak is left as it is."""
    if not mapping or not os.path.exists(steam_env.SHORTCUTS_PATH): return 0
    try:
        with _SHORTCUTS_LOCK:
            os.makedirs(appfiles.BACKUP_FOLDER, exist_ok=True)
            bak = _shortcuts_backup_path()
            if not os.path.exists(bak): shutil.copy2(steam_env.SHORTCUTS_PATH, bak)
            with open(steam_env.SHORTCUTS_PATH, "rb") as f: data = vdf.binary_loads(f.read())
            changed = 0
            for _key, game in data.get("shortcuts", {}).items():
                aid = game.get("appid")
                if aid is None: continue
                uid = aid & 0xFFFFFFFF
                if uid in mapping:
                    game["icon"] = mapping[uid]; changed += 1
            if changed:
                _write_shortcuts_atomic(data)
            return changed
    except Exception as e:
        print(f"Failed to set icons: {e}"); return 0

def set_shortcut_icon(unsigned_id, icon_path):
    """Write an icon path into shortcuts.vdf for the given app. Backs up on first write.
    Note: Steam should be closed when this runs to prevent it overwriting the change.
    Delegates to set_shortcut_icons so there's one atomic code path; returns bool to
    preserve its existing contract for callers (app.py on-demand apply)."""
    return set_shortcut_icons({unsigned_id: icon_path}) > 0

# --- Pending (deferred) icon writes ----------------------------------------------
# See PENDING_ICONS_FILE above for the design rationale (defer when Steam is open,
# auto-apply when it closes — no dialog, no Steam restart by us). All helpers below
# are pure file I/O, tkinter-free, and never raise so the UI poll is safe.
def _pending_path():
    """Return the ACTIVE account's pending-icons path, migrating any legacy
    un-namespaced file into it along the way (a deliberate one-time side effect, so
    every caller observes a single consistent, migrated path).

    WHY per-account: pending icons are written into the ACTIVE account's shortcuts.vdf,
    so the queue MUST be namespaced per account. A single shared queue let account A's
    icons be applied against account B's shortcuts.vdf (no matching appids) and then
    cleared unconditionally — silently losing A's icons on any account switch. We read
    the LIVE STEAM_USER_ID global so a set_active_user() switch is honored immediately,
    and fall back to the un-namespaced base path when no account is resolved."""
    base = appfiles.PENDING_ICONS_FILE
    if not steam_env.STEAM_USER_ID:
        return base
    namespaced = f"{base}_{steam_env.STEAM_USER_ID}"
    # A v1.3.0 queue (un-namespaced) belongs to whichever account is active at upgrade
    # time; move it under that account so it isn't orphaned forever. Best-effort.
    try:
        if os.path.exists(base) and not os.path.exists(namespaced):
            os.replace(base, namespaced)
    except Exception:
        _debug("_pending_path migrate")
    return namespaced

def save_pending_icons(mapping):
    """MERGE `mapping` ({unsigned_id:int -> icon_path:str}) into the active account's
    pending file and write it back as JSON (keys stored as strings). Never raises."""
    if not mapping: return
    try:
        merged = {str(k): v for k, v in load_pending_icons().items()}
        merged.update({str(k): v for k, v in mapping.items()})
        with open(_pending_path(), "w", encoding="utf-8") as f:
            json.dump(merged, f)
    except Exception as e:
        print(f"Failed to save pending icons: {e}")

def load_pending_icons():
    """Return {int_uid: path} from the active account's pending file. {} if
    missing/unparseable. Never raises."""
    try:
        with open(_pending_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except Exception:
        return {}

def clear_pending_icons():
    """Remove the active account's pending-icons file if present. Never raises."""
    try:
        path = _pending_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Failed to clear pending icons: {e}")

def has_pending_icons():
    """Cheap existence check (active account) so the UI poll can early-out without
    reading/parsing."""
    return os.path.exists(_pending_path())

def apply_pending_icons():
    """Safe flush of deferred icon writes. Returns the count written (0 if it did nothing).
    Does NOTHING (returns 0, leaves the pending file intact) if Steam is running, if there
    are no pending icons, or if SHORTCUTS_PATH doesn't exist — so we never write while Steam
    could clobber it. Otherwise: load pending, drop entries whose icon path no longer exists
    on disk, set_shortcut_icons(...) the rest, then clear_pending_icons() and return the count.

    We only clear the pending file when we actually reach a write attempt with Steam closed
    (never when we skipped because Steam was running). If filtering leaves nothing valid to
    write, we still clear: those icon files are gone, so there's nothing to retry. Never raises."""
    try:
        if steam_env.is_steam_running(): return 0
        pending = load_pending_icons()
        if not pending or not os.path.exists(steam_env.SHORTCUTS_PATH): return 0
        # "" is a valid queued value: Undo restores "this game had no icon" by
        # queueing exactly "", and os.path.exists("") is False would drop it.
        valid = {uid: path for uid, path in pending.items() if path == "" or os.path.exists(path)}
        n = set_shortcut_icons(valid) if valid else 0
        clear_pending_icons()  # reached a real write attempt while Steam was closed
        return n
    except Exception as e:
        print(f"Failed to apply pending icons: {e}"); return 0


def download_artwork(url, save_path, register=True):
    # Download to a temp file and only swap it into place once it's complete, so a
    # dropped connection can never leave a truncated image that gets applied/registered.
    tmp = save_path + ".part"
    try:
        r = sgdb.http_get(url, stream=True, timeout=10)
        if r.status_code != 200:
            print(f"    ❌ Failed ({r.status_code}): {url}"); return False
        written = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1024):
                f.write(chunk); written += len(chunk)
        expected = r.headers.get("Content-Length")
        # A compressed response (Steam's icon CDN gzips .ico files) states the
        # COMPRESSED length while iter_content yields decoded bytes, so the byte
        # count only means something for an unencoded body. A short compressed body
        # is still caught: urllib3 enforces Content-Length on the raw stream and
        # raises IncompleteRead (enforce_content_length defaults to True).
        encoded = r.headers.get("Content-Encoding", "identity").lower() not in ("", "identity")
        if not encoded and expected and expected.isdigit() and written != int(expected):
            raise IOError(f"incomplete download: {written}/{expected} bytes")
        os.replace(tmp, save_path)  # atomic within the same folder
        if register:
            appfiles.register_managed_file(save_path)
        print(f"    ✅ Saved: {os.path.basename(save_path)}"); return True
    except Exception as e:
        _debug("download_artwork")
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except OSError: pass
        print(f"    ❌ Network error: {e}"); return False

def clear_slot_files(save_path, existing_files=None, keep=None):
    """Delete any existing artwork sharing `save_path`'s grid slot but using a
    different extension. Steam keys art off the filename base (e.g. '12345p'), so a
    leftover '12345p.png' would keep displaying instead of a new '12345p.webp' —
    this guarantees the freshly downloaded file is the only one Steam sees.
    The previous file is already preserved by snapshot_for_restore() before this runs.

    existing_files: optional pre-listed folder snapshot to avoid a redundant listdir
    (download_all_artwork shares ONE snapshot across this game's slots — safe because
    each slot has a distinct filename base). Standalone callers leave it None.
    keep: a filename to leave alone — install_into_slot passes the file it has just
    installed, which an older snapshot lists under the same name as the old art."""
    folder = os.path.dirname(save_path)
    prefix = os.path.basename(save_path).rsplit(".", 1)[0] + "."
    if existing_files is None:
        try:
            existing_files = os.listdir(folder)
        except OSError:
            return
    for f in existing_files:
        if f.startswith(prefix) and f != keep:
            try: os.remove(os.path.join(folder, f))
            except OSError: pass

def remove_partial_downloads(grid_folder, unsigned_ids):
    """Delete the .part files a stopped fetch left behind: in `grid_folder`, the
    ones whose leading digit run is one of `unsigned_ids` (never another game's),
    and the matching preview downloads in CACHE_FOLDER. A .part is only ever this
    app's own unfinished download, never the user's art. Returns the number
    removed. Never raises."""
    ids = {str(i) for i in unsigned_ids}
    removed = 0
    for folder in (grid_folder, appfiles.CACHE_FOLDER):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for f in names:
            if not f.endswith(".part"):
                continue
            m = re.match(r"(\d+)", f)
            if m and m.group(1) in ids:
                try:
                    os.remove(os.path.join(folder, f))
                    removed += 1
                except OSError:
                    _debug("remove_partial_downloads")
    return removed

def install_into_slot(save_path, url, existing_files=None, animated=False,
                      progress_cb=None, status_cb=None):
    """The ONE way a new image lands in a grid slot — used by the fetch, by the
    results window's "Apply this one" and by the animated (APNG) apply. First get the
    new file into place atomically: a static image through download_artwork (a .part
    file, a length check, then os.replace onto save_path); an animated one through
    _apng_into_place (download + encode in the cache, then .part + os.replace). Only
    when that succeeded clear the slot's other extensions and register the file. A
    failure changes nothing in the grid folder: the old art stays (B5 — clearing
    first used to delete it even when the download then failed).
    Order note: the replace comes BEFORE the clear, so a crash between the two leaves
    the new file plus an old one of another extension, never an empty slot.
    progress_cb / status_cb are the APNG encode callbacks. Returns True when the
    new file is in place."""
    if animated:
        if not _apng_into_place(url, save_path, progress_cb, status_cb):
            return False
    elif not download_artwork(url, save_path, register=False):
        return False
    clear_slot_files(save_path, existing_files=existing_files,
                     keep=os.path.basename(save_path))
    appfiles.register_managed_file(save_path)
    return True

def apply_icon(unsigned_id, icon_path):
    """Point a shortcut's icon at icon_path in shortcuts.vdf: written at once while
    Steam is closed, QUEUED in the pending-icon file while it runs (Steam rewrites
    shortcuts.vdf on exit and would drop the write; the poll applies the queue once
    Steam closes). Returns True when the icon was queued, False when written."""
    if steam_env.is_steam_running():
        save_pending_icons({unsigned_id: icon_path})
        return True
    set_shortcut_icon(unsigned_id, icon_path)
    return False

def install_option(save_path, url, unsigned_id, is_icon=False, animated=False,
                   progress_cb=None, status_cb=None):
    """The results window's "Apply this one", static or animated: install the
    option into its slot (install_into_slot), then, for the icon slot, point
    Steam's shortcut at it (apply_icon). An animated icon is an APNG served as
    image/png; it goes through the same icon write as a static one (C4: the
    animated apply used to place the file and never touch shortcuts.vdf).
    Returns (ok, queued): ok when the file is in place, queued when the icon
    write waits for Steam to close."""
    ok = install_into_slot(save_path, url, animated=animated,
                           progress_cb=progress_cb, status_cb=status_cb)
    queued = False
    if ok and is_icon:
        queued = apply_icon(unsigned_id, save_path)
    return ok, queued

def row_thumbnail_path(grid_folder, app_id):
    """Return the best representative art file for `app_id` to show as a list-row
    thumbnail, or None if the game has no cover art on disk.

    Slot preference (Steam keys art off the filename base):
      1. portrait cover  "{app_id}p.<ext>"   (preferred — rows are portrait-shaped)
      2. wide grid       "{app_id}.<ext>"     (fallback)
    Hero ("{app_id}_hero.*"), logo ("{app_id}_logo.*") and icon ("{app_id}_icon.*")
    slots are intentionally ignored — they don't read well at row size.

    Pure / tkinter-free so it can be unit-tested headless. Returns an absolute path
    using whichever extension is present (png/jpg/webp/…); if several extensions
    share a slot, the lexicographically-first is returned for determinism."""
    try:
        names = os.listdir(grid_folder)
    except OSError:
        return None

    portrait_prefix = f"{app_id}p."
    wide_prefix = f"{app_id}."

    portrait, wide = [], []
    for name in names:
        if name.startswith(portrait_prefix):
            portrait.append(name)
        elif name.startswith(wide_prefix):
            # Exclude hero/logo/icon slots — their base is "{app_id}_…", which does
            # NOT start with "{app_id}." so they're already excluded here. This branch
            # only catches the true wide slot "{app_id}.<ext>".
            wide.append(name)

    chosen = sorted(portrait)[0] if portrait else (sorted(wide)[0] if wide else None)
    if chosen is None:
        return None
    return os.path.join(grid_folder, chosen)


def is_animated_image(img):
    """True if an SGDB image record refers to animated artwork. SGDB serves animated
    grids/heroes as webp/gif; static art is png/jpg."""
    return img.get("mime", "") in ("image/webp", "image/gif")

class _ApngFrameCounter:
    """Wraps the output file so we can report real per-frame progress during Pillow's
    otherwise-opaque APNG encode. Pillow writes each frame's 8-byte 'fcTL' chunk header
    as a discrete write() right before compressing that frame's (slow) data, so counting
    those headers tracks encode progress linearly in real time."""
    def __init__(self, fp, total, progress_cb):
        self._fp = fp
        self._total = total
        self._cb = progress_cb
        self._count = 0
    def write(self, b):
        if self._cb is not None and len(b) == 8 and bytes(b[-4:]) == b"fcTL":
            self._count += 1
            self._cb(self._count, self._total)
        return self._fp.write(b)
    def __getattr__(self, name):
        return getattr(self._fp, name)

def convert_animated_to_apng(src_path, out_path, progress_cb=None, status_cb=None):
    """Convert an animated webp/gif at src_path to APNG at out_path (.png) — the only
    animated format Steam's library renders. (Steam ignores .webp/.gif in the grid
    folder, so animated covers must be APNG.) progress_cb(done, total) fires per frame
    during the slow encode; status_cb(message) reports the current phase. Returns True
    on success."""
    from PIL import Image
    im = Image.open(src_path)
    n = getattr(im, "n_frames", 1)
    if status_cb: status_cb("Reading frames…")
    frames, durations = [], []
    for i in range(n):
        im.seek(i)
        durations.append(im.info.get("duration", 60))
        frames.append(im.convert("RGBA"))
    if status_cb: status_cb("Converting to APNG…")
    with open(out_path, "wb") as raw:
        fp = _ApngFrameCounter(raw, n, progress_cb)
        frames[0].save(fp, format="PNG", save_all=True,
                       append_images=frames[1:], duration=durations, loop=0, disposal=2)
    return True

def _apng_into_place(url, out_path, progress_cb=None, status_cb=None):
    """Download an animated webp/gif from `url`, convert it to APNG and put it at
    out_path. The source download and the slow encode (seconds to minutes) both
    happen in CACHE_FOLDER, never in Steam's grid folder (B7: encoding straight into
    the live file left a half-written PNG for Steam to read if the app closed). The
    finished APNG is copied next to out_path as a .part and os.replace'd into place —
    a copy, because the cache and the grid folder may be on different drives.
    progress_cb(done, total) reports per-frame encode progress; status_cb(message)
    reports the current phase. Leaves no temp file behind. Returns True on success."""
    if status_cb:
        status_cb("Downloading…")
    os.makedirs(appfiles.CACHE_FOLDER, exist_ok=True)
    fd, work = tempfile.mkstemp(prefix="apng_", dir=appfiles.CACHE_FOLDER)
    os.close(fd)
    src = f"{work}.src.{url.split('.')[-1].split('?')[0]}"
    encoded = f"{work}.png"
    part = out_path + ".part"
    try:
        if not download_artwork(url, src, register=False):
            return False
        convert_animated_to_apng(src, encoded, progress_cb, status_cb)
        shutil.copyfile(encoded, part)
        os.replace(part, out_path)
        return True
    except Exception as e:
        _debug("_apng_into_place")
        print(f"    ❌ APNG conversion failed: {e}"); return False
    finally:
        for p in (work, src, encoded, part):
            try:
                if os.path.exists(p): os.remove(p)
            except OSError:
                pass


class FetchedArt:
    """What download_all_artwork did for one game, in two separate fields so no
    consumer can mistake one kind of value for the other (T-A6: one dict used to
    hold the slot dicts AND the deferred icon tuple, and a loop over its values
    crashed on the tuple and left the UI on "Fetching…").
      slots       {slot key: slot dict, or None when the slot found nothing}, in
                  slot order: grids, grids_wide, heroes, logos, icons.
      icon_write  (unsigned_id, icon_path) of an icon installed with
                  defer_icon=True, for the caller's batched shortcuts.vdf write;
                  None otherwise."""

    def __init__(self):
        self.slots = {}
        self.icon_write = None

    def applied_paths(self):
        """The applied_path of every slot that auto-applied a static image."""
        return [d["applied_path"] for d in self.slots.values()
                if d and d.get("applied_index") is not None and d.get("applied_path")]

    def failed_labels(self):
        """The label ("Cover", "Icon", …) of every slot whose download failed."""
        return [d.get("label") or key for key, d in self.slots.items()
                if d and d.get("failed")]

def download_all_artwork(sgdb_id, unsigned_id, prefs=None, progress_cb=None, defer_icon=False):
    """Fetch and install one game's artwork from SGDB id `sgdb_id`; returns a
    FetchedArt. With defer_icon=True the icon's shortcuts.vdf write is left to the
    caller (FetchedArt.icon_write) so a whole run writes once."""
    if prefs is None:
        prefs = appfiles.load_prefs()
    # slot key -> (label, SGDB endpoint, filename base, dimensions filter)
    # Portrait and wide covers share the "grids" endpoint, split by dimensions.
    slots = {
        "grids":      ("Cover",           "grids",  f"{unsigned_id}p",     "600x900,660x930"),
        "grids_wide": ("Wide Cover",      "grids",  f"{unsigned_id}",      "460x215,920x430"),
        "heroes":     ("Hero/Background", "heroes", f"{unsigned_id}_hero", None),
        "logos":      ("Logo",            "logos",  f"{unsigned_id}_logo", None),
        "icons":      ("Icon",            "icons",  f"{unsigned_id}_icon", None),
    }
    # One GRID_FOLDER snapshot reused by snapshot_for_restore + every clear_slot_files
    # below, instead of ~6 listdir calls per game. Safe to share within ONE game: each
    # slot has a distinct filename base, so a slot's freshly written file never affects
    # another slot's clear. (Taken fresh per call — never shared across games.)
    try:
        existing_files = os.listdir(steam_env.GRID_FOLDER)
    except OSError:
        existing_files = []
    snapshot_for_restore(unsigned_id, existing_files=existing_files)
    os.makedirs(appfiles.CACHE_FOLDER, exist_ok=True)
    results = FetchedArt()
    # N slots × (1 main + 3 previews) steps max
    total_steps = len(slots) * 4
    step = 0
    # Progress is bumped from concurrent preview threads, so guard the counter.
    step_lock = threading.Lock()

    def bump(label):
        nonlocal step
        with step_lock:
            step += 1
            cur = step
        if progress_cb:
            progress_cb(label, cur, total_steps)

    # --- Phase 1: fetch all slots' metadata concurrently (bounded) -------------
    # Each get_artwork is an independent, network-bound round-trip, so running the
    # five together collapses ~5 serial round-trips into ~1.
    def fetch_slot(item):
        slot, (art_label, endpoint, base, dimensions) = item
        return slot, sgdb.get_artwork(sgdb_id, endpoint, prefs, dimensions=dimensions)

    # The Steam-link lookup is one more independent round-trip, so it rides in the
    # same pool as the five slot fetches.
    with ThreadPoolExecutor(max_workers=sgdb.SGDB_MAX_WORKERS) as ex:
        steam_future = ex.submit(sgdb.get_steam_platform_data, sgdb_id)
        slot_images = dict(ex.map(fetch_slot, slots.items()))
        steam_link = steam_future.result()
    steam_images = sgdb.steam_official_images(*steam_link) if steam_link else {}

    # --- Phase 2: per slot (declared order, so results/progress stay deterministic),
    # download the applied art then the previews (previews run concurrently) -----
    for slot, (art_label, endpoint, base, dimensions) in slots.items():
        images = list(slot_images.get(slot) or [])
        # Steam's own store asset always goes FIRST (so it is the auto-applied pick),
        # even when SGDB has nothing for this slot. The only thing that drops it is a
        # Logo/Icon "Official = Exclude" pref (_strip_never on its "official" style).
        official = sgdb._strip_never([steam_images[slot]], endpoint, prefs) if slot in steam_images else []
        images = official + images
        if not images:
            # A missing slot still consumes its 1 main + 3 preview steps so the bar
            # reaches 100% exactly (same accounting as the serial version).
            for _ in range(4):
                bump(f"{art_label} — none found")
            results.slots[slot] = None
            continue
        # Keep SGDB's original ranking so animated options stay visible in the results
        # (don't resort static-first — that can push animated past the 5-option cap).
        # Animated art is NOT auto-applied (Steam needs a slow APNG conversion), so we
        # auto-apply the first STATIC option and leave animated ones as alternatives
        # the user commits on demand.
        options = images[:5]
        applied_index = next((i for i, im in enumerate(options)
                              if not is_animated_image(im)), None)

        # Target path: the auto-applied static file keeps its own extension; if there
        # is nothing to auto-apply (all options animated), the slot stays empty until
        # the user applies one, which will be written as APNG (.png).
        failed = False
        if applied_index is not None:
            top = options[applied_index]
            ext = top["url"].split(".")[-1].split("?")[0]
            save_path = os.path.join(steam_env.GRID_FOLDER, f"{base}.{ext}")
            bump(f"Downloading {art_label}")
            if not install_into_slot(save_path, top["url"], existing_files=existing_files):
                # The download failed and the old art is untouched. Nothing was
                # applied, so applied_index is None (FetchedArt.applied_paths skips
                # the slot) and no icon write may point Steam at a missing file.
                failed = True
                applied_index = None
            elif slot == "icons":
                if defer_icon:
                    # Defer the shortcuts.vdf write so the caller can batch + write all
                    # icons in ONE atomic pass (and while Steam is closed).
                    results.icon_write = (unsigned_id, save_path)
                else:
                    set_shortcut_icon(unsigned_id, save_path)
        else:
            save_path = os.path.join(steam_env.GRID_FOLDER, f"{base}.png")
            bump(f"{art_label} — animated, apply to commit")

        # Previews: first 3 options, downloaded at FULL resolution (img["url"]) using
        # the original {base}_{i} cache names — unchanged from the serial version, so
        # animated previews still animate and app.py's on-demand cycling path (which
        # reconstructs {base}_{i} names) stays valid. They write to DISTINCT paths, so
        # downloading them concurrently is safe.
        preview_imgs = list(enumerate(options[:3]))

        def fetch_preview(arg):
            i, img = arg
            u = img["url"]; e = u.split(".")[-1].split("?")[0]
            tp = os.path.join(appfiles.CACHE_FOLDER, f"{base}_{i}.{e}")
            bump(f"Caching {art_label} preview {i+1}/3")
            download_artwork(u, tp, register=False)
            return i, tp

        if preview_imgs:
            with ThreadPoolExecutor(max_workers=sgdb.SGDB_MAX_WORKERS) as ex:
                pairs = list(ex.map(fetch_preview, preview_imgs))
            # Re-sort by index so thumb_paths still lines up with options 0..2.
            thumbs = [tp for _i, tp in sorted(pairs, key=lambda p: p[0])]
        else:
            thumbs = []

        results.slots[slot] = {"applied_path": save_path,
                               "applied_index": applied_index,
                               "failed": failed,
                               "label": art_label,
                               "option_urls": [img["url"] for img in options],
                               "option_meta": [
                                   {
                                       "animated": is_animated_image(img),
                                       "nsfw":     bool(img.get("nsfw", False)),
                                       "humor":    bool(img.get("humor", False)),
                                       "steam":    bool(img.get("steam_official", False)),
                                   }
                                   for img in options
                               ],
                               "thumb_paths": thumbs, "filename_base": base}
    return results

# ---------------------------------------------------------------------------
# Window-placement helpers — pure arithmetic, no tkinter dependency.
# Extracted here so they can be unit-tested without a display.
# ---------------------------------------------------------------------------

def parse_net_workarea(xprop_output):
    """Parse the first desktop's (x, y, w, h) from an ``xprop -root _NET_WORKAREA``
    output string.

    A typical line looks like::

        _NET_WORKAREA(CARDINAL) = 0, 0, 1920, 1053, 0, 0, 1920, 1053

    The first four integers after the ``=`` are x, y, w, h for desktop 0.
    Returns a ``(x, y, w, h)`` tuple of ints, or ``None`` when the string is
    empty, contains the "not found" sentinel, or has fewer than four numbers.
    """
    if not xprop_output or not xprop_output.strip():
        return None
    # xprop writes "_NET_WORKAREA:  not found." when the property is absent.
    if "not found" in xprop_output.lower():
        return None
    # Take everything after the first "=" so we skip the property-name header.
    after_eq = xprop_output.split("=", 1)[-1]
    # Parse signs honestly so a negative origin is reported as-is and the caller
    # (which sanity-checks for non-negative coords) can reject it, rather than a
    # stripped minus turning it into a wrong-but-positive value.
    nums = [int(n) for n in re.findall(r"-?\d+", after_eq)]
    if len(nums) < 4:
        return None
    return (nums[0], nums[1], nums[2], nums[3])


def compute_window_fit(wa_x, wa_y, wa_w, wa_h, desired_w, desired_h, reserve=80):
    """Return ``(w, h, x, y)`` so a window of *desired* size fits inside the
    given work area without overlapping a taskbar or panel.

    Parameters
    ----------
    wa_x, wa_y : int
        Top-left corner of the work area (may be non-zero if a panel sits on
        the left or top).
    wa_w, wa_h : int
        Width and height of the work area.
    desired_w, desired_h : int
        Preferred window dimensions.  The window only shrinks below these when
        the work area is too small.
    reserve : int
        Vertical pixels reserved for the window-manager title bar so that the
        bottom edge of the client area never lands under the taskbar on small
        screens.  Defaults to 80.

    Returns
    -------
    (final_w, final_h, x, y) : tuple[int, int, int, int]
        The size and top-left position to pass to ``window.geometry()``.
        Every edge is guaranteed to lie within the work area.
    """
    avail_h = max(wa_h - reserve, 200)
    # Cap to the actual work area as well, so even on a degenerate screen smaller
    # than the 200px reserve floor the window can never exceed (and overflow) the
    # work area — keeping the "every edge inside the work area" guarantee.
    final_w = max(1, min(desired_w, wa_w))
    final_h = max(1, min(desired_h, avail_h, wa_h))
    # Centre horizontally; vertically leave reserve/2 of slack above and below
    # so neither the title bar nor the bottom buttons land under a panel/taskbar.
    x = wa_x + (wa_w - final_w) // 2
    y = wa_y + (reserve // 2) + (avail_h - final_h) // 2
    # Clamp every edge inside the work area.
    x = max(wa_x, min(x, wa_x + wa_w - final_w))
    y = max(wa_y, min(y, wa_y + wa_h - final_h))
    return final_w, final_h, x, y


# ---------------------------------------------------------------------------
# Update check — pure network helper, no tkinter dependency.
# ---------------------------------------------------------------------------

# Single source of truth for the repo's releases page — used both as a manual
# fallback link in the UI and (implicitly) by the asset-picking logic below.
RELEASES_URL = "https://github.com/Rhastago/NonSteamScraper/releases"


def _rate_limit_reason(headers):
    """Build the rate-limit error string, adding the minutes until the limit resets
    when GitHub supplies it. GitHub returns `X-RateLimit-Reset` (UNIX epoch seconds)
    on a rate-limited 403; some proxies use `Retry-After` (seconds). Parsing is fully
    defensive — a missing/garbage header just falls back to the generic message."""
    try:
        reset = headers.get("X-RateLimit-Reset")
        if reset:
            secs = int(reset) - int(time.time())
            if secs > 0:
                mins = max(1, (secs + 59) // 60)  # ceil without importing math
                return f"GitHub rate limit reached — try again in ~{mins} min."
        retry_after = headers.get("Retry-After")
        if retry_after:
            secs = int(retry_after)
            if secs > 0:
                mins = max(1, (secs + 59) // 60)
                return f"GitHub rate limit reached — try again in ~{mins} min."
    except Exception:
        pass
    return "GitHub rate limit reached — try again later."


def _version_tuple(s):
    """Convert a version string like "1.2.3" or "v1.2.5-beta" to a comparable
    int tuple. Strips a leading "v" and any non-numeric suffix on each part."""
    s = s.lstrip("v").strip()
    parts = []
    for p in s.split("."):
        m = re.match(r"(\d+)", p)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts) if parts else (0,)


def check_for_update(current_version):
    """Check GitHub releases for a newer version.

    Returns a dict on both success and failure — never raises, never returns None.

    Success::
        {"available": bool, "latest": str, "current": str, "url": str, "error": None}

    Failure::
        {"available": False, "latest": None, "current": current_version,
         "url": None, "error": "<short human reason>"}

    Retry policy: retries ONCE only on RequestException (network/timeout), with a
    1 s pause.  Non-200 HTTP responses are returned immediately (no retry) so that
    rate-limit errors don't burn more of the 60/hr unauthenticated GitHub budget.
    """
    _UA = "NonSteamScraper-update-check"
    _HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": _UA}
    _URL = "https://api.github.com/repos/Rhastago/NonSteamScraper/releases/latest"

    def _err(reason):
        return {"available": False, "latest": None, "current": current_version,
                "url": None, "error": reason}

    for attempt in range(2):  # initial try + one retry (network errors only)
        if attempt:
            time.sleep(1)
        try:
            r = sgdb.http_get(_URL, headers=_HEADERS, timeout=10)
            if r.status_code != 200:
                # Do NOT retry non-200 — return the reason immediately.
                _debug("check_for_update")
                if r.status_code == 403:
                    return _err(_rate_limit_reason(getattr(r, "headers", {}) or {}))
                return _err(f"GitHub returned HTTP {r.status_code}.")
            data = r.json()
            raw_tag = data.get("tag_name", "")
            latest = raw_tag.lstrip("v").strip()
            if not latest:
                return _err("Unexpected response from GitHub.")

            available = _version_tuple(latest) > _version_tuple(current_version)

            # Pick the most appropriate asset for the current platform.
            assets = data.get("assets", [])
            plat = platform.system()  # "Linux", "Windows", "Darwin", …
            url = data.get("html_url", "")  # fallback: release page

            if assets:
                name_lower = [a["name"].lower() for a in assets]
                if plat == "Windows":
                    # prefer an asset whose name contains "win", else .zip or .exe
                    for keyword in ("win", ".zip", ".exe"):
                        for i, nl in enumerate(name_lower):
                            if keyword in nl:
                                url = assets[i]["browser_download_url"]
                                break
                        else:
                            continue
                        break
                elif plat == "Linux":
                    # prefer an asset whose name contains "linux", else the one
                    # with no file extension (bare binary)
                    matched = None
                    for i, nl in enumerate(name_lower):
                        if "linux" in nl:
                            matched = assets[i]["browser_download_url"]
                            break
                    if matched is None:
                        for i, a in enumerate(assets):
                            if "." not in a["name"]:
                                matched = a["browser_download_url"]
                                break
                    if matched:
                        url = matched
                # Darwin / other: fall through to html_url (release page)

            return {"available": available, "latest": latest,
                    "current": current_version, "url": url, "error": None}
        except requests.exceptions.RequestException:
            _debug("check_for_update")
            # Network error — retry once, then report reason.
        except Exception:
            _debug("check_for_update")
            return _err("Unexpected response from GitHub.")  # parse error, no retry

    return _err("Couldn't reach GitHub (network error).")


if __name__ == "__main__":
    key = appfiles.load_api_key()
    if not key: print("❌ No API key set."); exit()
    print(f"API key: {key[:6]}...\n")
    appfiles.clean_old_cache()
    games = get_non_steam_games()
    needs = [g for g in games if not g["has_art"]]
    print(f"Found {len(games)} games, {len(needs)} need artwork\n")
    for game in needs:
        print(f"🎮 {game['name']}")
        outcome, sid = sgdb.search_game(game["name"], game["app_id"])
        if outcome == sgdb.SEARCH_FOUND: download_all_artwork(sid, game["app_id"])
        else: print("  ⚠️ Not found")
        print()
    print("✅ Done! Restart Steam.")
