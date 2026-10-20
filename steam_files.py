"""Steam's own files: shortcuts.vdf (read the non-Steam games, write icons
through ONE locked, atomic writer, back up each account's file before its
first write) and the per-account pending-icon queue that holds icon writes
while Steam runs — the same lock guards both. tkinter-free.

Every path is the ACTIVE account's, read live from steam_env at call time."""

import json
import os
import re
import shutil
import tempfile
import threading
import vdf
from appcommon import debug as _debug
import appfiles
import steam_env


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
    """MB of the art copies still sitting in the backup folder from every past
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
    """Delete the old art copies in the backup folder, never a shortcuts.vdf
    backup. Returns the number of files that could not be removed (B14)."""
    if not os.path.exists(appfiles.BACKUP_FOLDER): return 0
    paths = [os.path.join(appfiles.BACKUP_FOLDER, f) for f in os.listdir(appfiles.BACKUP_FOLDER)
             if not _is_shortcuts_backup(f)]
    return len(appfiles.remove_files([p for p in paths if os.path.isfile(p)], "clear_backup")[1])

def cover_uids(names):
    """The app ids (as strings) that have a COVER in a grid-folder listing: a
    portrait cover <id>p.<ext> or a wide one <id>.<ext> — the files the list row
    shows as the game's cover (artwork.row_thumbnail_path). A game needs artwork
    while it has none (Q2): a lone hero, logo or icon does not count, and a .part
    (an unfinished download) never counts (N7: either one used to hide a game from
    "needs artwork" for good). The id is the file's LEADING DIGIT RUN, never a
    prefix match: 123456p.png is game 123456, not 12345."""
    uids = set()
    for fname in names:
        if fname.endswith(".part"):
            continue
        m = re.match(r"(\d+)p?\.", fname)
        if m:
            uids.add(m.group(1))
    return uids

class ShortcutsUnreadable(Exception):
    """shortcuts.vdf exists but cannot be read or parsed — Steam may be writing it
    this very moment, or it is damaged (N12)."""

def get_non_steam_games():
    """The non-Steam games in the active account's shortcuts.vdf, each with its
    has_art / skipped state. [] when the file does not exist. Raises
    ShortcutsUnreadable when it exists but cannot be read or parsed: the caller
    shows that, instead of the app crashing at startup with no message (N12)."""
    if not os.path.exists(steam_env.SHORTCUTS_PATH): return []
    try:
        with open(steam_env.SHORTCUTS_PATH, "rb") as f:
            data = vdf.binary_loads(f.read())
        shortcuts = data.get("shortcuts", {})
        if not isinstance(shortcuts, dict):
            raise ValueError("no shortcuts table")
    except Exception as e:
        raise ShortcutsUnreadable(str(e)) from e
    skip_list = appfiles.load_skip_list(); seen = set(); games = []

    # One grid-folder listing for the whole library (O(files), not O(games ×
    # files)); cover_uids decides which games have a cover. If GRID_FOLDER doesn't
    # exist or listdir fails, no game has art and has_art falls through to the
    # skip-list check without crashing.
    try:
        uids_with_art = cover_uids(os.listdir(steam_env.GRID_FOLDER))
    except OSError:
        uids_with_art = set()

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

# ONE lock for shortcuts.vdf AND the pending-icon queue. The fetch worker, the 4 s
# pending-icon poll, the results window's apply flows and Undo all write them, from
# two threads; each does read -> change -> write, so without a lock two of them could
# read the same original and the second write would drop the first one's icons (B4).
# The queue is part of it (N3): the poll's load -> write -> drop must not interleave
# with a direct write, or an OLDER queued icon lands on top of a newer direct one.
# Re-entrant: apply_pending_icons holds it across its call to set_shortcut_icons.
_ICON_LOCK = threading.RLock()

def _write_shortcuts_atomic(data):
    """Serialize `data` and atomically replace shortcuts.vdf: write a UNIQUE temp file
    (tempfile.mkstemp) in the SAME directory, fsync it, then os.replace() onto
    SHORTCUTS_PATH. os.replace is atomic within one filesystem, so a crash/clobber
    mid-write can never leave a truncated vdf; the fsync makes sure the bytes are on
    disk before the rename (a power loss right after a write on ext4 can otherwise
    leave an empty file). The caller holds _ICON_LOCK."""
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
    count of shortcuts changed — or None when the write FAILED (unreadable or damaged
    file, a file held open on Windows): never 0 for a failure, so a caller can tell
    "nothing matched" from "nothing written" (N2). Never raises.
    After a successful write every uid in `mapping` leaves the active account's icon
    queue: this write is newer than anything queued for those games, and the poll must
    never flush an older queued icon over it (N3).
    The backup is per account (shortcuts.vdf.<userid>.bak), taken on that account's
    first write; the legacy shortcuts.vdf.bak is left as it is."""
    if not mapping or not os.path.exists(steam_env.SHORTCUTS_PATH): return 0
    try:
        with _ICON_LOCK:
            os.makedirs(appfiles.BACKUP_FOLDER, exist_ok=True)
            bak = _shortcuts_backup_path()
            if not os.path.exists(bak): _backup_atomic(steam_env.SHORTCUTS_PATH, bak)
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
            _drop_queued(mapping)
            return changed
    except Exception as e:
        print(f"Failed to set icons: {e}"); return None

def _backup_atomic(src, bak):
    """Copy src to bak through a unique temp file next to bak and one os.replace,
    so a crash mid-copy can never leave a truncated backup that the "only on the
    first write" rule would then keep for good (N15)."""
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(bak) + ".", suffix=".tmp",
                               dir=os.path.dirname(bak))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, bak)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

def set_shortcut_icon(unsigned_id, icon_path):
    """Write an icon path into shortcuts.vdf for the given app. Backs up on first write.
    Note: Steam should be closed when this runs to prevent it overwriting the change.
    Delegates to set_shortcut_icons so there's one atomic code path. Returns True when
    a shortcut was changed, False when none matched, None when the write failed."""
    n = set_shortcut_icons({unsigned_id: icon_path})
    return None if n is None else n > 0

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
    pending file and write it back as JSON (keys stored as strings). Holds _ICON_LOCK
    across the read-merge-write. Never raises."""
    if not mapping: return
    try:
        with _ICON_LOCK:
            merged = {str(k): v for k, v in load_pending_icons().items()}
            merged.update({str(k): v for k, v in mapping.items()})
            # Atomic (B15): a truncated queue loads as {} and the icons are lost.
            appfiles.write_text_atomic(_pending_path(), json.dumps(merged))
    except Exception as e:
        print(f"Failed to save pending icons: {e}")

def _drop_queued(uids):
    """Take `uids` out of the active account's pending-icon queue, removing the file
    when that empties it (as clear_pending_icons does). Holds _ICON_LOCK. Never
    raises."""
    try:
        with _ICON_LOCK:
            path = _pending_path()
            if not os.path.exists(path):
                return
            pending = load_pending_icons()
            drop = {int(u) for u in uids}
            keep = {k: v for k, v in pending.items() if k not in drop}
            if len(keep) == len(pending):
                return
            if keep:
                appfiles.write_text_atomic(path, json.dumps({str(k): v for k, v in keep.items()}))
            else:
                os.remove(path)
    except Exception as e:
        print(f"Failed to update pending icons: {e}")

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
        with _ICON_LOCK:
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
    """Safe flush of deferred icon writes. Returns the count written (0 if it did
    nothing), or None when the shortcuts.vdf write FAILED — then the whole queue stays
    for the next try (N2: it used to be cleared anyway, and the icons were lost while
    the UI said they applied).
    Does NOTHING (returns 0, leaves the pending file intact) if Steam is running, if there
    are no pending icons, or if SHORTCUTS_PATH doesn't exist — so we never write while Steam
    could clobber it. Otherwise: load pending, drop entries whose icon path no longer exists
    on disk, set_shortcut_icons(...) the rest, then take out of the queue exactly the
    entries this flush handled (written, matching no shortcut, or whose file is gone —
    nothing left to retry for those). _ICON_LOCK is held from the load to that removal,
    so an icon queued or written meanwhile is never lost or overwritten (N3). Never
    raises."""
    try:
        with _ICON_LOCK:
            if steam_env.is_steam_running(): return 0
            pending = load_pending_icons()
            if not pending or not os.path.exists(steam_env.SHORTCUTS_PATH): return 0
            # "" is a valid queued value: Undo restores "this game had no icon" by
            # queueing exactly "", and os.path.exists("") is False would drop it.
            valid = {uid: path for uid, path in pending.items() if path == "" or os.path.exists(path)}
            n = set_shortcut_icons(valid) if valid else 0
            if n is None:
                return None
            _drop_queued(pending)
            return n
    except Exception as e:
        print(f"Failed to apply pending icons: {e}"); return None

def write_or_queue_icons(mapping):
    """The icon write at the end of a fetch and of an Undo: QUEUED while Steam runs
    (it rewrites shortcuts.vdf on exit and would drop the write), written now while it
    is closed — and queued after all when that write fails, so the 4 s poll retries it
    instead of the icons being lost (N2). Returns (written, queued): written is the
    count of shortcuts changed (None when the write failed), queued is True when the
    icons wait in the queue."""
    if not mapping:
        return 0, False
    if steam_env.is_steam_running():
        save_pending_icons(mapping)
        return 0, True
    n = set_shortcut_icons(mapping)
    if n is None:
        save_pending_icons(mapping)
        return None, True
    return n, False

def apply_icon(unsigned_id, icon_path):
    """Point a shortcut's icon at icon_path in shortcuts.vdf: written at once while
    Steam is closed, QUEUED in the pending-icon file while it runs (Steam rewrites
    shortcuts.vdf on exit and would drop the write; the poll applies the queue once
    Steam closes). Returns True when the icon was queued, False when written, None
    when the write failed (the results window then says "Failed — retry")."""
    if steam_env.is_steam_running():
        save_pending_icons({unsigned_id: icon_path})
        return True
    if set_shortcut_icon(unsigned_id, icon_path) is None:
        return None
    return False
