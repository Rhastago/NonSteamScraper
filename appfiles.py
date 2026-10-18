"""The app's own files under the user's home: ONE registry of every
~/.steamart_* path, the atomic text write they all go through, and the small
load/save helpers for each (API key, skip list, name and SGDB-id overrides,
art-style prefs, the managed-file registry, the preview cache), plus
full_reset. tkinter-free.

Paths are module globals bound at import (expanduser), so tests sandbox HOME
before importing and patch a path HERE, on the module that owns it. Readers
elsewhere use appfiles.NAME at call time, never a by-value import."""

import glob
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timedelta

from appcommon import debug as _debug


# --- The registry: every file and folder the app keeps under HOME --------------
# full_reset, the README "Data stored locally" table and the tests all work from
# this list; a path defined anywhere else is a second copy that can drift (H2).
APIKEY_FILE    = os.path.expanduser("~/.steamart_apikey")
PREFS_FILE     = os.path.expanduser("~/.steamart_prefs")
THEME_FILE     = os.path.expanduser("~/.steamart_theme")
ACCENT_FILE    = os.path.expanduser("~/.steamart_accent")
ACCOUNT_FILE   = os.path.expanduser("~/.steamart_account")
FIRSTRUN_FILE  = os.path.expanduser("~/.steamart_firstrun")
GEOMETRY_FILE  = os.path.expanduser("~/.steamart_geometry")
SKIP_FILE      = os.path.expanduser("~/.steamart_skip")
NAMES_FILE     = os.path.expanduser("~/.steamart_names")
# The SteamGridDB game id the user picked in Search → Select, per app id
# ("appid|sgdb_id" per line). A separate file, not a new form in NAMES_FILE: an
# older build reads that file with split("|", 1) and would take an id as a name.
IDS_FILE       = os.path.expanduser("~/.steamart_ids")
MANAGED_FILE   = os.path.expanduser("~/.steamart_managed")
CACHE_FOLDER   = os.path.expanduser("~/.steamart_cache")
BACKUP_FOLDER  = os.path.expanduser("~/.steamart_backup")
RESTORE_FOLDER = os.path.expanduser("~/.steamart_restore")
# Icons are only safe to write into shortcuts.vdf while Steam is CLOSED (Steam
# rewrites the file on exit and would clobber our change). When a fetch finishes
# with Steam open we therefore DEFER the icon writes to this file instead of
# prompting and restarting Steam ourselves. WHY no dialog/restart:
#   - the old prompt was marshaled from the fetch worker thread to the UI thread
#     and could hang forever (the dialog didn't reliably appear), and
#   - auto-restarting (stop → sleep(3) → write → start) was racy — Steam shutting
#     down gracefully could rewrite shortcuts.vdf and clobber our icon.
# Instead the app polls and flushes these pending icons the moment Steam is closed.
# One file per Steam account: steam_files adds the "_<account id>" suffix.
PENDING_ICONS_FILE = os.path.expanduser("~/.steamart_pending_icons")


def write_text_atomic(path, text):
    """Replace `path` with `text` (UTF-8) atomically: a unique temp file in the
    same folder, fsync, then os.replace. A crash mid-write leaves the old file or
    the new one, never a truncated one (B15: a truncated pending-icon file loaded
    as {} and silently dropped the queued icons). An existing file keeps its mode.
    The folder must exist."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                shutil.copymode(path, tmp)
            except OSError:
                _debug("write_text_atomic copymode")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --- API key ---------------------------------------------------------------------
# Invariant: _apikey_cache holds (path, mtime_or_None, value).
#   - On each load_api_key() call we stat APIKEY_FILE.
#   - If the stat succeeds and mtime equals the cached mtime, return the
#     cached value (fast path — no file open).
#   - If the mtime differs (file changed) or is None (file appeared after a
#     previous miss), we re-read the file and update the cache.
#   - If stat raises (file missing/unreadable), we cache (path, None, "") so
#     a persistently missing file isn't re-stat-thrashed, but a new file that
#     later appears will have a different mtime and force a re-read.
# A threading.Lock serialises reads from the concurrent SGDB worker threads.
_apikey_lock  = threading.Lock()
_apikey_cache = (None, None, "")   # (path, mtime, value)

def load_api_key():
    global _apikey_cache
    path = APIKEY_FILE
    with _apikey_lock:
        cached_path, cached_mtime, cached_value = _apikey_cache
        # Fast path: stat the file and compare mtime.
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = None   # file missing or unreadable
        if cached_path == path and cached_mtime == mtime:
            return cached_value   # cache hit (mtime unchanged, or still missing)
        # Cache miss — read the file (or return "" if missing).
        try:
            with open(path, encoding="utf-8") as f:
                value = f.read().strip()
        except Exception:
            _debug("load_api_key")
            value = ""
        _apikey_cache = (path, mtime, value)
        return value

def save_api_key(key):
    # Write the file, then update the cache so the new key is visible
    # immediately without a round-trip through the mtime check.
    global _apikey_cache
    write_text_atomic(APIKEY_FILE, key.strip())
    with _apikey_lock:
        try:
            mtime = os.stat(APIKEY_FILE).st_mtime
        except OSError:
            mtime = None
        _apikey_cache = (APIKEY_FILE, mtime, key.strip())


# --- Skip list, name and id overrides ----------------------------------------------

def load_skip_list():
    if not os.path.exists(SKIP_FILE): return set()
    with open(SKIP_FILE, encoding="utf-8") as f: return set(l.strip() for l in f if l.strip())

def add_to_skip_list(app_id):
    if str(app_id) in load_skip_list():
        return
    text = ""
    if os.path.exists(SKIP_FILE):
        with open(SKIP_FILE, encoding="utf-8") as f:
            text = f.read()
        if text and not text.endswith("\n"):
            text += "\n"
    write_text_atomic(SKIP_FILE, text + f"{app_id}\n")

def remove_from_skip_list(app_id):
    """Take one app id off the skip list (Restore, Re-fetch, Search → Select)."""
    if not os.path.exists(SKIP_FILE):
        return
    with open(SKIP_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    write_text_atomic(SKIP_FILE, "".join(l for l in lines if l.strip() != str(app_id)))

def load_name_overrides():
    o = {}
    if not os.path.exists(NAMES_FILE): return o
    with open(NAMES_FILE, encoding="utf-8") as f:
        for l in f:
            l = l.strip()
            if "|" in l:
                a, n = l.split("|", 1); o[a.strip()] = n.strip()
    return o

def save_name_override(app_id, name):
    o = load_name_overrides(); o[str(app_id)] = name
    write_text_atomic(NAMES_FILE, "".join(f"{a}|{n}\n" for a, n in o.items()))

def load_id_overrides():
    """{app_id_str: sgdb_id_int} from IDS_FILE. A malformed line is skipped."""
    o = {}
    if not os.path.exists(IDS_FILE): return o
    with open(IDS_FILE, encoding="utf-8") as f:
        for l in f:
            a, _, i = l.strip().partition("|")
            if a.strip() and i.strip().isdigit():
                o[a.strip()] = int(i.strip())
    return o

def save_id_override(app_id, sgdb_id):
    o = load_id_overrides(); o[str(app_id)] = int(sgdb_id)
    write_text_atomic(IDS_FILE, "".join(f"{a}|{i}\n" for a, i in o.items()))


# --- Managed-file registry ------------------------------------------------------------

def register_managed_file(path):
    """Record a file this app wrote (Clear All Artwork and Undo may delete only
    these). A path already listed is not added again (B13)."""
    if path in load_managed_files():
        return
    with open(MANAGED_FILE, "a", encoding="utf-8") as f: f.write(f"{path}\n")

def prune_managed_files():
    """Rewrite the registry without duplicate lines and without files that no
    longer exist (B13: every apply, swap and undo appended a line, and nothing
    ever removed one). A file whose FOLDER is missing is kept — an unmounted
    drive must not make the app forget its own files. Touches only the registry,
    never the files. Returns the number of lines dropped. Never raises."""
    try:
        if not os.path.exists(MANAGED_FILE):
            return 0
        with open(MANAGED_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        keep = [p for p in dict.fromkeys(lines)
                if os.path.exists(p) or not os.path.isdir(os.path.dirname(p))]
        if len(keep) != len(lines):
            write_text_atomic(MANAGED_FILE, "".join(f"{p}\n" for p in keep))
        return len(lines) - len(keep)
    except Exception:
        _debug("prune_managed_files")
        return 0

def load_managed_files():
    if not os.path.exists(MANAGED_FILE): return set()
    with open(MANAGED_FILE, encoding="utf-8") as f: return set(l.strip() for l in f if l.strip())


# --- Art-style preferences -------------------------------------------------------------

DEFAULT_PREFS = {
    "animated": "never", "nsfw": "never", "humor": "ok",
    "grid_no_logo": "ok", "grid_blurred": "ok", "grid_alternate": "ok", "grid_material": "ok",
    "hero_alternate": "ok", "hero_blurred": "ok",
    "logo_official": "ok", "logo_white": "ok", "logo_black": "ok",
    "icon_official": "ok",
}

def load_prefs():
    if not os.path.exists(PREFS_FILE):
        return dict(DEFAULT_PREFS)
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        prefs = dict(DEFAULT_PREFS)
        prefs.update({k: v for k, v in loaded.items() if k in DEFAULT_PREFS})
        return prefs
    except Exception:
        _debug("load_prefs")
        return dict(DEFAULT_PREFS)

def save_prefs(prefs):
    write_text_atomic(PREFS_FILE, json.dumps(prefs, indent=2))


# --- Preview cache -----------------------------------------------------------------------

def get_cache_size():
    if not os.path.exists(CACHE_FOLDER): return 0.0
    total = sum(os.path.getsize(os.path.join(CACHE_FOLDER, f))
                for f in os.listdir(CACHE_FOLDER)
                if os.path.isfile(os.path.join(CACHE_FOLDER, f)))
    return round(total / (1024 * 1024), 2)

def remove_files(paths, context):
    """Delete each path, going on past a failure (B14: one locked file — on
    Windows, an image open in a viewer — used to stop a cleanup half-way, out of
    a Tk callback). Returns (removed, failed_paths)."""
    removed, failed = 0, []
    for p in paths:
        try:
            os.remove(p)
            removed += 1
        except OSError:
            _debug(context)
            failed.append(p)
    return removed, failed

def clear_cache():
    """Empty the preview cache. Returns the number of files that could not be
    removed."""
    if not os.path.exists(CACHE_FOLDER): return 0
    paths = [os.path.join(CACHE_FOLDER, f) for f in os.listdir(CACHE_FOLDER)]
    return len(remove_files([p for p in paths if os.path.isfile(p)], "clear_cache")[1])

def clean_old_cache(days=30):
    """Drop cached previews older than `days`. Returns the number that could not
    be removed."""
    if not os.path.exists(CACHE_FOLDER): return 0
    cutoff = datetime.now() - timedelta(days=days)
    old = []
    for f in os.listdir(CACHE_FOLDER):
        p = os.path.join(CACHE_FOLDER, f)
        try:
            if os.path.isfile(p) and datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
                old.append(p)
        except OSError:
            _debug("clean_old_cache stat")
    return len(remove_files(old, "clean_old_cache")[1])


# --- Factory reset ------------------------------------------------------------------------

def full_reset():
    """Reset the app to a first-launch state WITHOUT touching the user's fetched/
    applied artwork. Clears SETTINGS only — API key, prefs, theme/accent, skip list,
    name overrides, picked SGDB ids, saved account, window geometry, first-run
    flag — plus the regenerable thumbnail cache and any pending (un-applied) icon
    queue.

    DELIBERATELY PRESERVES the applied grid artwork, the managed-file registry
    (~/.steamart_managed), the artwork backup and the restore point
    (~/.steamart_restore) so a settings reset can NEVER lose the user's art
    collection. Removing art is the job of the separate `clear_managed_artwork()`
    ("Clear All Artwork") action only — not this one."""
    clear_cache()
    files = [APIKEY_FILE, PREFS_FILE, SKIP_FILE, NAMES_FILE, IDS_FILE, ACCOUNT_FILE,
             FIRSTRUN_FILE, THEME_FILE, ACCENT_FILE, GEOMETRY_FILE]
    # Pending-icon queues are per-account (namespaced), so sweep the whole family
    # plus any legacy un-namespaced file. (Applied icons in shortcuts.vdf are kept.)
    files += glob.glob(PENDING_ICONS_FILE + "*")
    for f in files:
        try:
            if os.path.exists(f): os.remove(f)
        except Exception:
            pass
