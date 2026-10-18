"""Steam's own files: shortcuts.vdf (read the non-Steam games, write icons
through ONE locked, atomic writer, back up each account's file before its
first write) and the per-account pending-icon queue that holds icon writes
while Steam runs. tkinter-free.

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
    """Delete the old art copies in ~/.steamart_backup, never a shortcuts.vdf
    backup. Returns the number of files that could not be removed (B14)."""
    if not os.path.exists(appfiles.BACKUP_FOLDER): return 0
    paths = [os.path.join(appfiles.BACKUP_FOLDER, f) for f in os.listdir(appfiles.BACKUP_FOLDER)
             if not _is_shortcuts_backup(f)]
    return len(appfiles.remove_files([p for p in paths if os.path.isfile(p)], "clear_backup")[1])

def get_non_steam_games():
    if not os.path.exists(steam_env.SHORTCUTS_PATH): return []
    with open(steam_env.SHORTCUTS_PATH, "rb") as f:
        data = vdf.binary_loads(f.read())
    shortcuts = data.get("shortcuts", {})
    skip_list = appfiles.load_skip_list(); seen = set(); games = []

    # --- Single grid-folder scan with boundary-correct matching ---------------
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
        # Atomic (B15): a truncated queue loads as {} and the icons are lost.
        appfiles.write_text_atomic(_pending_path(), json.dumps(merged))
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
