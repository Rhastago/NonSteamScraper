"""The app's own files: ONE folder that holds them all (DATA_FOLDER), ONE
registry of every path inside it, the move of an older version's files out of
the home folder, the atomic text write they all go through, and the small
load/save helpers for each (API key, skip list, name and SGDB-id overrides,
art-style prefs, the managed-file registry, the preview cache), plus
full_reset. tkinter-free.

Paths are module globals bound at import (from HOME / APPDATA), so tests
sandbox those before importing and patch a path HERE, on the module that owns
it. Readers elsewhere use appfiles.NAME at call time, never a by-value import.
Importing creates nothing: app.py calls ensure_data_folder() at startup."""

import filecmp
import glob
import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta

from appcommon import debug as _debug


# --- Where the app keeps its files -------------------------------------------------

def data_folder():
    """The one folder for every app file (R1): %APPDATA%\\NonSteamScraper on Windows,
    ~/.config/NonSteamScraper everywhere else. XDG_CONFIG_HOME is deliberately not
    read (Q3): it changes with whatever starts the app (inside a Flatpak it points
    into that sandbox), and the folder must be the same on every launch."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"),
                                                         "AppData", "Roaming")
        return os.path.join(base, "NonSteamScraper")
    return os.path.join(os.path.expanduser("~"), ".config", "NonSteamScraper")

DATA_FOLDER = data_folder()

def ensure_data_folder():
    """Create DATA_FOLDER. app.py calls this first, before the one-copy lock:
    acquire_instance_lock lets the app start when the lock file cannot be opened,
    so a missing folder would silently switch one-copy-at-a-time off. Returns
    True when the folder exists. Never raises."""
    try:
        os.makedirs(DATA_FOLDER, exist_ok=True)
        return True
    except OSError as e:
        print(f"Failed to create {DATA_FOLDER}: {e}")
        return False


# --- The registry: every file and folder the app keeps -------------------------
# full_reset, migrate_legacy_files and the tests all work from this list; a path
# defined anywhere else is a second copy that can drift (H2). Up to v1.5.2 each
# lived in the home folder as ~/.steamart_<name>; the name inside DATA_FOLDER is
# that same <name>.
APIKEY_FILE    = os.path.join(DATA_FOLDER, "apikey")
PREFS_FILE     = os.path.join(DATA_FOLDER, "prefs")
THEME_FILE     = os.path.join(DATA_FOLDER, "theme")
ACCENT_FILE    = os.path.join(DATA_FOLDER, "accent")
ACCOUNT_FILE   = os.path.join(DATA_FOLDER, "account")
FIRSTRUN_FILE  = os.path.join(DATA_FOLDER, "firstrun")
GEOMETRY_FILE  = os.path.join(DATA_FOLDER, "geometry")
SKIP_FILE      = os.path.join(DATA_FOLDER, "skip")
NAMES_FILE     = os.path.join(DATA_FOLDER, "names")
# The SteamGridDB game id the user picked in Search → Select, per app id
# ("appid|sgdb_id" per line). A separate file, not a new form in NAMES_FILE: an
# older build reads that file with split("|", 1) and would take an id as a name.
IDS_FILE       = os.path.join(DATA_FOLDER, "ids")
MANAGED_FILE   = os.path.join(DATA_FOLDER, "managed")
CACHE_FOLDER   = os.path.join(DATA_FOLDER, "cache")
BACKUP_FOLDER  = os.path.join(DATA_FOLDER, "backup")
RESTORE_FOLDER = os.path.join(DATA_FOLDER, "restore")
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
PENDING_ICONS_FILE = os.path.join(DATA_FOLDER, "pending_icons")
# Held (OS file lock) by the one running copy of the app; NOT in full_reset's list
# — deleting it while held would let a second copy start.
LOCK_FILE      = os.path.join(DATA_FOLDER, "lock")


# --- One copy at a time -----------------------------------------------------------
# Two open copies share the icon queue and Steam's shortcut list, and the
# shortcuts.vdf lock only makes writes take turns INSIDE one process, so two
# copies writing at the same instant could lose a change (operator ruling W8).
_instance_lock = None   # the open lock file, kept for the life of the process

def _try_lock(f):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

def acquire_instance_lock(wait=3.0):
    """Take the one-copy lock. Returns True when this process now holds it, False
    when another copy does. Retries for up to `wait` seconds: a theme/accent
    relaunch replaces the process, and on Windows the old one can still be exiting
    when the new one starts. The OS releases the lock when the process ends, even
    after a crash, so it can never get stuck. Never raises."""
    global _instance_lock
    deadline = time.monotonic() + wait
    while True:
        try:
            f = open(LOCK_FILE, "a+")
        except OSError:
            _debug("acquire_instance_lock open")
            return True   # no lock file possible (e.g. unwritable HOME): never block the app
        try:
            _try_lock(f)
        except OSError:
            # Held by another copy (BlockingIOError on Linux, PermissionError on Windows).
            f.close()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
            continue
        except Exception:
            _debug("acquire_instance_lock")
            f.close()
            return True   # an unexpected failure must never stop the app
        _instance_lock = f
        return True

def release_instance_lock():
    """Let go of the one-copy lock (tests; the OS does it at exit)."""
    global _instance_lock
    if _instance_lock is not None:
        _instance_lock.close()
        _instance_lock = None


# --- Moving an older version's files out of the home folder (R1, R2) ---------------
# Up to v1.5.2 every file lived in the home folder as ~/.steamart_<name>. These are
# the names migrate_legacy_files moves, by name — never a glob of ~/.steamart_*,
# which would also catch a crash-left temp file. The pending-icon queues are a
# family (pending_icons, pending_icons_<account id>), matched exactly by
# _PENDING_NAME. The lock is not moved: it is removed (Q5).
LEGACY_PREFIX = ".steamart_"
_LEGACY_FILES   = ("apikey", "prefs", "theme", "accent", "account", "firstrun",
                   "geometry", "skip", "names", "ids", "managed")
_LEGACY_FOLDERS = ("cache", "backup", "restore")
_PENDING_NAME   = re.compile(r"^pending_icons(_\d+)?$")

# The last migrate_legacy_files summary, for the window to report once it is built.
LAST_MIGRATION = None

def _copy_into_place(src, dst):
    """Copy src (a file or a folder) to dst through a temp name next to dst and
    one os.replace, so a copy that fails half-way never leaves a partial dst that
    the next launch would take for the real thing. Raises OSError."""
    tmp = dst + ".part"
    try:
        if os.path.isdir(src):
            shutil.copytree(src, tmp)
        else:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        elif os.path.exists(tmp):
            os.remove(tmp)
        raise

def _move(src, dst):
    """Move src to the free name dst: a rename, else (other drive, a file held
    open on Windows) a copy that keeps the original (Q4 — nothing is deleted).
    Returns "moved", "copied" or "failed"."""
    try:
        os.replace(src, dst)
        return "moved"
    except OSError:
        _debug("migrate rename")
    try:
        _copy_into_place(src, dst)
        return "copied"
    except OSError as e:
        print(f"Failed to move {src}: {e}")
        return "failed"

def _merge_managed(src, dst):
    """Both registries exist (Q6): dst becomes the union of the two, its own lines
    first, and the merged-in src goes. Returns "merged" or "failed"."""
    try:
        lines = []
        for p in (dst, src):
            with open(p, encoding="utf-8") as f:
                lines += [l.strip() for l in f if l.strip()]
        write_text_atomic(dst, "".join(f"{p}\n" for p in dict.fromkeys(lines)))
        os.remove(src)   # its every line is now in dst
        return "merged"
    except OSError as e:
        print(f"Failed to merge {src}: {e}")
        return "failed"

def _merge_folder(src, dst):
    """Both cache or backup folders exist: move each file of src that dst does
    not have (never overwrite), then drop src if that left it empty. Returns
    "merged" when src is gone, "kept" when some of it stays."""
    try:
        names = os.listdir(src)
    except OSError:
        return "kept"
    for name in names:
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isfile(s) and not os.path.exists(d):
            _move(s, d)
    try:
        os.rmdir(src)   # only ever succeeds on an empty folder
        return "merged"
    except OSError:
        return "kept"

def _same(src, dst):
    """True when the old item holds nothing the new one lacks: an identical file,
    or a folder whose every file is in dst with identical content — what a copy
    that replaced a failed move (Q4) leaves behind. Never raises."""
    try:
        if os.path.isfile(src):
            return os.path.isfile(dst) and filecmp.cmp(src, dst, shallow=False)
        for root, _dirs, files in os.walk(src):
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(dst, os.path.relpath(s, src))
                if not (os.path.isfile(d) and filecmp.cmp(s, d, shallow=False)):
                    return False
        return True
    except OSError:
        return False

def migrate_legacy_files(home=None, folder=None):
    """Move the files an older version kept in the home folder (~/.steamart_<name>)
    into the data folder, item by item. Runs at startup after the one-copy lock
    and before steam_env.init() reads the saved account.

    - An older copy still running holds ~/.steamart_lock: then nothing is moved
      this launch (it would keep writing the old files). The old lock is held
      while moving, so an older copy cannot start half-way through.
    - A name the data folder does not have yet is renamed into it; when the rename
      fails it is copied and the original kept (Q4).
    - A name the data folder already has: the new copy wins and the old one is
      left alone — except the managed-file registry, whose lines are merged (Q6),
      and the cache and backup folders, whose missing files are moved in.
    - Afterwards the old lock file is removed when it is empty and no older copy
      held it (Q5, the one deletion the operator ruled).
    Idempotent: what moved is gone from the home folder, and a failure is retried
    next launch. An old item identical to the new one (the original a copy left
    behind) is "same" and is not reported again. Returns a summary {moved, copied,
    merged, kept, same, failed: [names], skipped: None or the reason, lock_removed:
    bool}, also kept in LAST_MIGRATION.
    Never raises."""
    global LAST_MIGRATION
    home = home or os.path.expanduser("~")
    folder = folder or DATA_FOLDER
    summary = {"moved": [], "copied": [], "merged": [], "kept": [], "same": [],
               "failed": [], "skipped": None, "lock_removed": False}
    LAST_MIGRATION = summary
    old_lock_path = os.path.join(home, LEGACY_PREFIX + "lock")
    old_lock = None
    try:
        if os.path.exists(old_lock_path):
            old_lock = open(old_lock_path, "a+")
            try:
                _try_lock(old_lock)
            except OSError:
                old_lock.close()
                summary["skipped"] = "an older copy of NonSteamScraper is running"
                print(f"Settings not moved: {summary['skipped']}.")
                return summary
        try:
            listing = os.listdir(home)
        except OSError:
            listing = []
        names = list(_LEGACY_FILES) + list(_LEGACY_FOLDERS)
        names += sorted(n[len(LEGACY_PREFIX):] for n in listing
                        if n.startswith(LEGACY_PREFIX)
                        and _PENDING_NAME.match(n[len(LEGACY_PREFIX):]))
        for name in names:
            src = os.path.join(home, LEGACY_PREFIX + name)
            dst = os.path.join(folder, name)
            if not os.path.exists(src):
                continue
            if not os.path.exists(dst):
                outcome = _move(src, dst)
            elif name == "managed":
                outcome = _merge_managed(src, dst)
            elif name in ("cache", "backup"):
                outcome = _merge_folder(src, dst)
            else:
                outcome = "kept"
            if outcome == "kept" and _same(src, dst):
                outcome = "same"
            summary[outcome].append(name)
        if old_lock is not None:
            old_lock.close()
            old_lock = None
            try:
                if os.path.getsize(old_lock_path) == 0:
                    os.remove(old_lock_path)
                    summary["lock_removed"] = True
            except OSError:
                _debug("migrate old lock")
    except Exception:
        _debug("migrate_legacy_files")
    finally:
        if old_lock is not None:
            old_lock.close()
    for key in ("moved", "copied", "merged", "kept", "failed"):
        if summary[key]:
            print(f"Settings migration — {key}: {', '.join(summary[key])}")
    return summary


def write_text_atomic(path, text):
    """Replace `path` with `text` (UTF-8) atomically: a unique temp file in the
    same folder, fsync, then os.replace. A crash mid-write leaves the old file or
    the new one, never a truncated one (B15: a truncated pending-icon file loaded
    as {} and silently dropped the queued icons). An existing file keeps its mode;
    a new one gets the usual mode for the user's umask, like a plain open() — the
    temp file is created with 0o666 (umask applied), not mkstemp's owner-only 0600.
    The folder must exist."""
    folder = os.path.dirname(path) or "."
    while True:
        tmp = os.path.join(folder, f"{os.path.basename(path)}.{secrets.token_hex(4)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
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

def unregister_managed_files(paths):
    """Take `paths` off the registry — files that are no longer this app's, such as
    the user's own art that Undo put back over a file the app had written under the
    same name (N1: it stayed listed, so a later Clear All Artwork deleted it). Touches
    only the registry. Raises OSError when the registry cannot be rewritten."""
    drop = set(paths)
    if not drop or not os.path.exists(MANAGED_FILE):
        return
    with open(MANAGED_FILE, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    keep = [p for p in lines if p not in drop]
    if len(keep) != len(lines):
        write_text_atomic(MANAGED_FILE, "".join(f"{p}\n" for p in keep))

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
    # How many options each artwork slot gets (results ◀/▶), every one previewed
    # during the fetch (operator rulings Q10/Q11). A string like every other pref.
    "options": "5",
    # Settings → "Full-size previews": "yes" brings back the pre-1.5.3 full-size
    # results previews instead of SteamGridDB thumbnails (ruling Q14b).
    "full_previews": "no",
}

OPTION_COUNTS = (1, 3, 5, 7, 10)

def option_count(prefs):
    """prefs["options"] as an int from OPTION_COUNTS; 5 for anything else (an older
    prefs file has no such key, a hand-edited one may hold junk)."""
    try:
        n = int(prefs.get("options", 5))
    except (TypeError, ValueError):
        return 5
    return n if n in OPTION_COUNTS else 5

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
    (MANAGED_FILE), the artwork backup and the restore point
    (RESTORE_FOLDER) so a settings reset can NEVER lose the user's art
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
