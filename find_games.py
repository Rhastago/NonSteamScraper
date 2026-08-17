import os, re, sys, platform, shutil, subprocess, time, json, threading, glob, vdf, requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote as _url_quote
from datetime import datetime, timedelta, timezone
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# Set STEAMART_DEBUG=1 to surface the detail behind the defensive `except` blocks.
DEBUG = os.environ.get("STEAMART_DEBUG", "").lower() in ("1", "true", "yes")

def _debug(context):
    """Print the current exception's traceback to stderr when STEAMART_DEBUG is set,
    otherwise stay silent. Call from inside an `except` block so failures that are
    intentionally swallowed can still be diagnosed without changing default behavior."""
    if DEBUG:
        import traceback
        print(f"[steamart] {context}:", file=sys.stderr)
        traceback.print_exc()

APIKEY_FILE = os.path.expanduser("~/.steamart_apikey")

# The app installs a callable taking one message string; find_games stays UI-free.
RATE_LIMIT_HOOK = None

# ---------------------------------------------------------------------------
# API-key mtime cache (Task #9)
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
# ---------------------------------------------------------------------------
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
    with open(APIKEY_FILE, "w", encoding="utf-8") as f:
        f.write(key.strip())
    with _apikey_lock:
        try:
            mtime = os.stat(APIKEY_FILE).st_mtime
        except OSError:
            mtime = None
        _apikey_cache = (APIKEY_FILE, mtime, key.strip())

def verify_api_key(key):
    if not key or not key.strip(): return False
    try:
        r = _http_get("https://www.steamgriddb.com/api/v2/search/autocomplete/test",
                       headers={"Authorization": f"Bearer {key.strip()}"}, timeout=8)
        return r.status_code == 200 and bool(r.json().get("success"))
    except Exception: return False

# One HTTP seam for the whole app: every outbound GET goes through _http_get,
# which draws from a single lazily-built, pooled requests.Session.  The app
# never mutates the session after creation (no per-request headers, no manual
# cookie writes), which is what makes it safe to share across the fetch
# ThreadPoolExecutor workers.
_SESSION = None
_SESSION_LOCK = threading.Lock()

def _get_session():
    """Lazily build and return the one shared requests.Session.

    Double-checked creation under _SESSION_LOCK so concurrent SGDB workers can
    race in safely while only one session is ever constructed.  The session is
    built and mounted on a local first and assigned to _SESSION last, so a
    reader can never observe a half-configured session.  SGDB_MAX_WORKERS is
    defined further down the module, so it is read here at call time, not at
    module scope."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                session = requests.Session()
                session.mount("https://", HTTPAdapter(
                    pool_connections=4, pool_maxsize=SGDB_MAX_WORKERS * 2))
                _SESSION = session
    return _SESSION

def _http_get(url, headers=None, params=None, timeout=None, stream=False):
    """The single GET seam.  No retry logic here (that stays in _sgdb_get) and
    no exception swallowing — callers handle both."""
    return _get_session().get(url, headers=headers, params=params,
                              timeout=timeout, stream=stream)


def _sgdb_get(url, headers, params=None, timeout=5, retries=2):
    """_http_get wrapper that retries on HTTP 429 (rate-limit).

    Sleeps for the value of the Retry-After response header (parsed as int
    seconds), falling back to a short backoff (2 s, then 4 s), capped at 5 s
    per attempt.  After exhausting retries the final response is returned as-is
    so callers can inspect it normally.  Never raises for a 429."""
    backoffs = [2, 4]
    r = _http_get(url, headers=headers, params=params, timeout=timeout)
    for attempt in range(retries):
        if r.status_code != 429:
            break
        try:
            wait = min(int(r.headers.get("Retry-After", backoffs[attempt])), 5)
        except (ValueError, TypeError):
            wait = min(backoffs[attempt], 5)
        if RATE_LIMIT_HOOK:
            try:
                RATE_LIMIT_HOOK(
                    f"SteamGridDB rate-limited — waiting {wait}s ({attempt + 1}/{retries})")
            except Exception:
                _debug("rate_limit_hook")
        time.sleep(wait)
        r = _http_get(url, headers=headers, params=params, timeout=timeout)
    return r


def _release_year(ts):
    """Convert a Unix timestamp to a 4-digit year string, or '' if absent."""
    try:
        return str(datetime.fromtimestamp(int(ts), tz=timezone.utc).year) if ts else ""
    except Exception:
        return ""


def search_sgdb_autocomplete(query):
    """Return list of {id, name} dicts from the SGDB autocomplete endpoint."""
    key = load_api_key()
    if not key or not query.strip():
        return []
    try:
        r = _sgdb_get(
            f"https://www.steamgriddb.com/api/v2/search/autocomplete/{_url_quote(query.strip())}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5,
        ).json()
        if r.get("success") and r.get("data"):
            return [
                {
                    "id": g["id"],
                    "name": g["name"],
                    "year": _release_year(g.get("release_date")),
                    "type": (g.get("types") or [""])[0],
                }
                for g in r["data"]
            ]
    except Exception:
        _debug("search_sgdb_autocomplete")
    return []


def _windows_steam_install():
    """Return Steam's install directory on Windows by reading the registry."""
    try:
        import winreg
        for hive in (r"SOFTWARE\WOW6432Node\Valve\Steam", r"SOFTWARE\Valve\Steam"):
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive)
                path, _ = winreg.QueryValueEx(key, "InstallPath")
                winreg.CloseKey(key)
                if path: return path
            except OSError:
                continue
    except ImportError:
        pass
    return None

def get_steam_path():
    s = platform.system()
    if s == "Linux":
        for p in [
            os.path.expanduser("~/.steam/steam/userdata"),
            os.path.expanduser("~/.local/share/Steam/userdata"),
            os.path.expanduser("~/.steam/root/userdata"),
        ]:
            if os.path.exists(p): return p
        return os.path.expanduser("~/.steam/steam/userdata")
    if s == "Windows":
        install = _windows_steam_install()
        if install:
            p = os.path.join(install, "userdata")
            if os.path.exists(p): return p
        for p in [os.path.expandvars(r"%PROGRAMFILES(X86)%\Steam\userdata"),
                  os.path.expandvars(r"%PROGRAMFILES%\Steam\userdata")]:
            if os.path.exists(p): return p
    if s == "Darwin": return os.path.expanduser("~/Library/Application Support/Steam/userdata")
    return None

def find_steam_user():
    udp = get_steam_path()
    if not udp or not os.path.exists(udp): return None, None
    for uid in os.listdir(udp):
        if uid != "0" and os.path.exists(os.path.join(udp, uid, "config", "shortcuts.vdf")):
            return udp, uid
    return None, None

def get_all_steam_users():
    udp = get_steam_path()
    if not udp or not os.path.exists(udp): return []
    return [u for u in os.listdir(udp)
            if u != "0" and os.path.exists(os.path.join(udp, u, "config", "shortcuts.vdf"))]

ACCOUNT_FILE  = os.path.expanduser("~/.steamart_account")

STEAM_USERDATA, STEAM_USER_ID = find_steam_user()
if STEAM_USER_ID is None:
    _udp = get_steam_path()
    if _udp and os.path.exists(_udp):
        _c = [u for u in os.listdir(_udp) if u != "0" and os.path.isdir(os.path.join(_udp, u))]
        if _c: STEAM_USERDATA, STEAM_USER_ID = _udp, _c[0]
# Prefer a previously chosen account (~/.steamart_account) if it's still valid.
# This only changes behavior when 2+ accounts exist; with 0 or 1 account the
# saved id either matches the sole account or is invalid, so the fallback stands.
if STEAM_USER_ID is not None:
    try:
        with open(ACCOUNT_FILE, encoding="utf-8") as _f:
            _saved = _f.read().strip()
        _udp = get_steam_path()
        if _saved and _udp and os.path.exists(os.path.join(_udp, _saved, "config", "shortcuts.vdf")):
            STEAM_USERDATA, STEAM_USER_ID = _udp, _saved
    except Exception:
        _debug("restore_active_user")
STEAM_NOT_FOUND = STEAM_USER_ID is None
if STEAM_NOT_FOUND:
    SHORTCUTS_PATH = GRID_FOLDER = ""
else:
    SHORTCUTS_PATH = os.path.join(STEAM_USERDATA, STEAM_USER_ID, "config", "shortcuts.vdf")
    GRID_FOLDER    = os.path.join(STEAM_USERDATA, STEAM_USER_ID, "config", "grid")
    os.makedirs(GRID_FOLDER, exist_ok=True)

def get_active_user():
    """Return the currently active userdata folder id (or None if Steam not found)."""
    return STEAM_USER_ID

def set_active_user(user_id):
    """Switch the active Steam account by rebinding the module globals
    (STEAM_USERDATA, STEAM_USER_ID, SHORTCUTS_PATH, GRID_FOLDER) and persisting
    the choice to ~/.steamart_account. Backend functions read these globals, so
    the switch takes effect for subsequent calls. Returns True on success, False
    if user_id is not a valid account folder with a shortcuts.vdf."""
    global STEAM_USERDATA, STEAM_USER_ID, SHORTCUTS_PATH, GRID_FOLDER
    udp = get_steam_path()
    if not udp or not user_id or not os.path.exists(os.path.join(udp, user_id, "config", "shortcuts.vdf")):
        return False
    STEAM_USERDATA = udp
    STEAM_USER_ID  = user_id
    SHORTCUTS_PATH = os.path.join(udp, user_id, "config", "shortcuts.vdf")
    GRID_FOLDER    = os.path.join(udp, user_id, "config", "grid")
    os.makedirs(GRID_FOLDER, exist_ok=True)
    try:
        with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
            f.write(user_id)
    except Exception:
        _debug("set_active_user persist")
    return True

def get_steam_user_personas():
    """Return {folder_user_id: persona_name} parsed from loginusers.vdf (text VDF).
    loginusers.vdf top-level keys are SteamID64 strings; the userdata folder id is
    the low 32 bits of that id (int(steamid64) & 0xFFFFFFFF). Ids without a known
    persona simply won't appear. Never raises."""
    personas = {}
    try:
        udp = get_steam_path()
        if not udp:
            return personas
        path = os.path.join(os.path.dirname(udp), "config", "loginusers.vdf")
        with open(path, encoding="utf-8") as f:
            data = vdf.loads(f.read())
        for sid64, info in (data.get("users") or {}).items():
            try:
                folder_id = str(int(sid64) & 0xFFFFFFFF)
                name = info.get("PersonaName")
                if name:
                    personas[folder_id] = name
            except (ValueError, TypeError, AttributeError):
                continue
    except Exception:
        _debug("get_steam_user_personas")
    return personas

SKIP_FILE     = os.path.expanduser("~/.steamart_skip")
NAMES_FILE    = os.path.expanduser("~/.steamart_names")
CACHE_FOLDER  = os.path.expanduser("~/.steamart_cache")
BACKUP_FOLDER = os.path.expanduser("~/.steamart_backup")
RESTORE_FOLDER = os.path.expanduser("~/.steamart_restore")
MANAGED_FILE  = os.path.expanduser("~/.steamart_managed")
PREFS_FILE    = os.path.expanduser("~/.steamart_prefs")
# Settings/state files cleared by full_reset (the UI-side app.py also references the
# same paths). Defined as constants so full_reset is monkeypatch-testable.
FIRSTRUN_FILE = os.path.expanduser("~/.steamart_firstrun")
THEME_FILE    = os.path.expanduser("~/.steamart_theme")
ACCENT_FILE   = os.path.expanduser("~/.steamart_accent")
GEOMETRY_FILE = os.path.expanduser("~/.steamart_geometry")
# Icons are only safe to write into shortcuts.vdf while Steam is CLOSED (Steam
# rewrites the file on exit and would clobber our change). When a fetch finishes
# with Steam open we therefore DEFER the icon writes to this file instead of
# prompting and restarting Steam ourselves. WHY no dialog/restart:
#   - the old prompt was marshaled from the fetch worker thread to the UI thread
#     and could hang forever (the dialog didn't reliably appear), and
#   - auto-restarting (stop → sleep(3) → write → start) was racy — Steam shutting
#     down gracefully could rewrite shortcuts.vdf and clobber our icon.
# Instead the app polls and flushes these pending icons the moment Steam is closed.
PENDING_ICONS_FILE = os.path.expanduser("~/.steamart_pending_icons")

DEFAULT_PREFS = {
    "animated": "never", "nsfw": "never", "humor": "ok",
    "grid_no_logo": "ok", "grid_blurred": "ok", "grid_alternate": "ok", "grid_material": "ok",
    "hero_alternate": "ok", "hero_blurred": "ok",
    "logo_official": "ok", "logo_white": "ok", "logo_black": "ok",
    "icon_official": "ok",
}

_STYLE_MAP = {
    "grids":  {"grid_no_logo": "no_logo", "grid_blurred": "blurred",
               "grid_alternate": "alternate", "grid_material": "material"},
    "heroes": {"hero_alternate": "alternateArt", "hero_blurred": "blurred"},
    "logos":  {"logo_official": "official", "logo_white": "white", "logo_black": "black"},
    "icons":  {"icon_official": "official"},
}

def load_skip_list():
    if not os.path.exists(SKIP_FILE): return set()
    with open(SKIP_FILE, encoding="utf-8") as f: return set(l.strip() for l in f if l.strip())

def add_to_skip_list(app_id):
    if str(app_id) not in load_skip_list():
        with open(SKIP_FILE, "a", encoding="utf-8") as f: f.write(f"{app_id}\n")

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
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        for a, n in o.items(): f.write(f"{a}|{n}\n")

def register_managed_file(path):
    with open(MANAGED_FILE, "a", encoding="utf-8") as f: f.write(f"{path}\n")

def load_managed_files():
    if not os.path.exists(MANAGED_FILE): return set()
    with open(MANAGED_FILE, encoding="utf-8") as f: return set(l.strip() for l in f if l.strip())

def clear_managed_artwork():
    managed = load_managed_files(); deleted = 0
    for p in managed:
        if os.path.exists(p): os.remove(p); deleted += 1
    if os.path.exists(MANAGED_FILE): os.remove(MANAGED_FILE)
    # The restore point snapshots the very art being cleared; leaving it behind
    # would let a later Undo silently re-copy and re-register everything the
    # operator just removed ("Clear All Artwork" promises to remove it all).
    clear_restore_point()
    return deleted

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
    with open(PREFS_FILE, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)

def get_cache_size():
    if not os.path.exists(CACHE_FOLDER): return 0.0
    total = sum(os.path.getsize(os.path.join(CACHE_FOLDER, f))
                for f in os.listdir(CACHE_FOLDER)
                if os.path.isfile(os.path.join(CACHE_FOLDER, f)))
    return round(total / (1024 * 1024), 2)

def clear_cache():
    if not os.path.exists(CACHE_FOLDER): return
    for f in os.listdir(CACHE_FOLDER):
        p = os.path.join(CACHE_FOLDER, f)
        if os.path.isfile(p): os.remove(p)

def clean_old_cache(days=30):
    if not os.path.exists(CACHE_FOLDER): return
    cutoff = datetime.now() - timedelta(days=days)
    for f in os.listdir(CACHE_FOLDER):
        p = os.path.join(CACHE_FOLDER, f)
        if os.path.isfile(p) and datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
            os.remove(p)

# --- Restore point (SPEC.md S3) --------------------------------------------------
# ~/.steamart_restore/ holds ONE restore point: manifest.json plus <id>/<file>
# byte copies (mtime preserved via copy2) taken before a fetch or Re-fetch
# destroys art, so Undo can put back what was there. The manifest is written
# atomically (temp file + os.replace), the same rule as _write_shortcuts_atomic
# and the download path. Every function here never raises (module contract).

def _restore_manifest_path():
    """Absolute path of the restore-point manifest. Lives inside RESTORE_FOLDER
    so monkeypatching RESTORE_FOLDER redirects the whole store (tests)."""
    return os.path.join(RESTORE_FOLDER, "manifest.json")

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
    os.makedirs(RESTORE_FOLDER, exist_ok=True)
    tmp = os.path.join(RESTORE_FOLDER, "manifest.json.tmp")
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
    if not os.path.exists(SHORTCUTS_PATH):
        return None
    try:
        with open(SHORTCUTS_PATH, "rb") as f:
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
        if m.get("grid_folder") and m.get("grid_folder") != GRID_FOLDER:
            shutil.rmtree(RESTORE_FOLDER, ignore_errors=True)
            m = {"version": 1, "grid_folder": "", "entries": {}}
        eid = str(unsigned_id)
        entry = m.get("entries", {}).get(eid)
        if entry is not None and not entry.get("sealed"):
            return  # unsealed entry wins — never re-snapshot over it
        if entry is not None:
            # sealed entry is superseded: its copies belong to an accepted run
            shutil.rmtree(os.path.join(RESTORE_FOLDER, eid), ignore_errors=True)
            del m["entries"][eid]
        managed = load_managed_files()
        if existing_files is None:
            try:
                existing_files = os.listdir(GRID_FOLDER)
            except OSError:
                existing_files = []
        entry_dir = os.path.join(RESTORE_FOLDER, eid)
        os.makedirs(entry_dir, exist_ok=True)
        files = []
        managed_subset = []
        for fname in _files_matching_id(existing_files, unsigned_id):
            src = os.path.join(GRID_FOLDER, fname)
            try:
                shutil.copy2(src, os.path.join(entry_dir, fname))
            except OSError:
                _debug("snapshot_for_restore copy")
                continue
            files.append(fname)
            if src in managed:
                managed_subset.append(fname)
        m["grid_folder"] = GRID_FOLDER
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
            shutil.rmtree(os.path.join(RESTORE_FOLDER, eid), ignore_errors=True)
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
        return bool(m.get("entries")) and m.get("grid_folder") == GRID_FOLDER
    except Exception:
        _debug("has_restore_point")
        return False

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
        managed = load_managed_files()
        icons = {}
        restored = 0
        removed = 0
        for eid, entry in m.get("entries", {}).items():
            # 1. delete current files in the managed registry matching this id
            try:
                for fname in _files_matching_id(os.listdir(GRID_FOLDER), eid):
                    cur = os.path.join(GRID_FOLDER, fname)
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
                dst = os.path.join(GRID_FOLDER, fname)
                try:
                    shutil.copy2(os.path.join(RESTORE_FOLDER, eid, fname), dst)
                    restored += 1
                except OSError:
                    _debug("undo_restore_point copy")
                    continue
                if fname in entry.get("managed", []):
                    try:
                        register_managed_file(dst)
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
            if is_steam_running():
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
    shutil.rmtree(RESTORE_FOLDER, ignore_errors=True)

def restore_point_size():
    """MB occupied by the restore store, rounded to 2 dp (mirrors
    get_cache_size). The store nests <id>/<file>, so this walks recursively."""
    if not os.path.exists(RESTORE_FOLDER):
        return 0.0
    total = 0
    try:
        for root, _dirs, files in os.walk(RESTORE_FOLDER):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        return 0.0
    return round(total / (1024 * 1024), 2)

def legacy_backup_size():
    """MB of the art copies still sitting in ~/.steamart_backup from every past
    fetch (backup_artwork is gone; this reports what it left behind), rounded to
    2 dp like get_cache_size. shortcuts.vdf.bak is EXCLUDED — it is never
    deleted (the only pre-app shortcut-list copy), so the Settings clear button
    could never zero it out. Never raises."""
    if not os.path.exists(BACKUP_FOLDER):
        return 0.0
    try:
        total = 0
        for f in os.listdir(BACKUP_FOLDER):
            if f == "shortcuts.vdf.bak": continue  # never deleted; not clearable
            p = os.path.join(BACKUP_FOLDER, f)
            if os.path.isfile(p):
                total += os.path.getsize(p)
    except OSError:
        return 0.0
    return round(total / (1024 * 1024), 2)

def clear_backup():
    if not os.path.exists(BACKUP_FOLDER): return
    for f in os.listdir(BACKUP_FOLDER):
        if f == "shortcuts.vdf.bak": continue
        p = os.path.join(BACKUP_FOLDER, f)
        if os.path.isfile(p): os.remove(p)

def full_reset():
    """Reset the app to a first-launch state WITHOUT touching the user's fetched/
    applied artwork. Clears SETTINGS only — API key, prefs, theme/accent, skip list,
    name overrides, saved account, window geometry, first-run flag — plus the
    regenerable thumbnail cache and any pending (un-applied) icon queue.

    DELIBERATELY PRESERVES the applied grid artwork, the managed-file registry
    (~/.steamart_managed), the artwork backup and the restore point
    (~/.steamart_restore) so a settings reset can NEVER lose the user's art
    collection. Removing art is the job of the separate `clear_managed_artwork()`
    ("Clear All Artwork") action only — not this one."""
    clear_cache()
    files = [APIKEY_FILE, PREFS_FILE, SKIP_FILE, NAMES_FILE, ACCOUNT_FILE,
             FIRSTRUN_FILE, THEME_FILE, ACCENT_FILE, GEOMETRY_FILE]
    # Pending-icon queues are per-account (namespaced), so sweep the whole family
    # plus any legacy un-namespaced file. (Applied icons in shortcuts.vdf are kept.)
    files += glob.glob(PENDING_ICONS_FILE + "*")
    for f in files:
        try:
            if os.path.exists(f): os.remove(f)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# is_steam_running TTL cache (Task #10)
# Invariant: _steam_cache holds (timestamp, bool_result).
#   - If the cached result is younger than _STEAM_RUNNING_TTL seconds, return
#     it immediately without iterating processes (cheap path for UI events).
#   - Otherwise, call psutil and refresh the cache.
# No lock needed: a float read/write is atomic on CPython, and a stale read
# in a tiny race window just causes one redundant psutil scan — not a hazard.
# ---------------------------------------------------------------------------
_STEAM_RUNNING_TTL = 2.5   # seconds between full process-list scans
_steam_cache = (0.0, False)  # (last_check_monotonic, last_result)

def is_steam_running(force=False):
    global _steam_cache
    if not PSUTIL_AVAILABLE:
        return False
    now = time.monotonic()
    last_ts, last_result = _steam_cache
    # force=True bypasses the TTL cache and always runs the psutil scan, then
    # refreshes the shared cache below. Callers use this right after stop_steam()
    # so a cached True can't linger up to _STEAM_RUNNING_TTL and make us think
    # Steam is still running. Refreshing _steam_cache here also means the very
    # next plain is_steam_running() (e.g. inside apply_pending_icons) sees the
    # same fresh False instead of the stale True.
    if not force and now - last_ts < _STEAM_RUNNING_TTL:
        return last_result   # cache still fresh — skip the process scan
    target = {"Linux": "steam", "Windows": "steam.exe", "Darwin": "steam"}.get(platform.system(), "steam").lower()
    try:
        result = any(p.info["name"] and p.info["name"].lower() == target
                     for p in psutil.process_iter(["name"]))
    except Exception:
        result = False
    _steam_cache = (now, result)
    return result

def stop_steam():
    """Kill the running Steam process (no relaunch). Factored out of restart_steam so
    callers can do stop → (write shortcuts.vdf) → start with the file write happening
    while Steam is closed and can't clobber it."""
    s = platform.system()
    try:
        if s == "Linux":     subprocess.Popen(["pkill", "-x", "steam"])
        elif s == "Windows": subprocess.Popen(["taskkill", "/F", "/IM", "steam.exe"])
        elif s == "Darwin":  subprocess.Popen(["pkill", "-x", "Steam"])
    except Exception as e: print(f"Failed to stop Steam: {e}")

def start_steam():
    """Launch Steam (no kill first). Pair with stop_steam() for a stop→write→start."""
    s = platform.system()
    try:
        if s == "Linux":     subprocess.Popen(["steam"])
        elif s == "Windows":
            install = _windows_steam_install() or r"C:\Program Files (x86)\Steam"
            subprocess.Popen([os.path.join(install, "steam.exe")])
        elif s == "Darwin":  subprocess.Popen(["open", "-a", "Steam"])
    except Exception as e: print(f"Failed to start Steam: {e}")

def restart_steam():
    # Unchanged behavior: stop, wait for the process to exit, then relaunch.
    stop_steam(); time.sleep(3); start_steam()

def get_non_steam_games():
    if not os.path.exists(SHORTCUTS_PATH): return []
    with open(SHORTCUTS_PATH, "rb") as f:
        data = vdf.binary_loads(f.read())
    shortcuts = data.get("shortcuts", {})
    skip_list = load_skip_list(); seen = set(); games = []

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
        for fname in os.listdir(GRID_FOLDER):
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

def _write_shortcuts_atomic(data):
    """Serialize `data` and atomically replace shortcuts.vdf: write a temp file in the
    SAME directory, then os.replace() onto SHORTCUTS_PATH. os.replace is atomic within
    one filesystem, so a crash/clobber mid-write can never leave a truncated vdf."""
    folder = os.path.dirname(SHORTCUTS_PATH)
    tmp = os.path.join(folder, "shortcuts.vdf.tmp")
    with open(tmp, "wb") as f:
        f.write(vdf.binary_dumps(data))
    os.replace(tmp, SHORTCUTS_PATH)

def set_shortcut_icons(mapping):
    """Batch-write icons into shortcuts.vdf in ONE read+parse+write instead of one full
    rewrite per game. `mapping` is {unsigned_id: icon_path}. Every shortcut whose
    (appid & 0xFFFFFFFF) is a key gets game["icon"] set (all shortcuts sharing an appid,
    like the single-write path). Backs up on first write; writes atomically. Returns the
    count of shortcuts changed. Never raises (matches set_shortcut_icon's style)."""
    if not mapping or not os.path.exists(SHORTCUTS_PATH): return 0
    try:
        os.makedirs(BACKUP_FOLDER, exist_ok=True)
        bak = os.path.join(BACKUP_FOLDER, "shortcuts.vdf.bak")
        if not os.path.exists(bak): shutil.copy2(SHORTCUTS_PATH, bak)
        with open(SHORTCUTS_PATH, "rb") as f: data = vdf.binary_loads(f.read())
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
    base = PENDING_ICONS_FILE
    if not STEAM_USER_ID:
        return base
    namespaced = f"{base}_{STEAM_USER_ID}"
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
        if is_steam_running(): return 0
        pending = load_pending_icons()
        if not pending or not os.path.exists(SHORTCUTS_PATH): return 0
        # "" is a valid queued value: Undo restores "this game had no icon" by
        # queueing exactly "", and os.path.exists("") is False would drop it.
        valid = {uid: path for uid, path in pending.items() if path == "" or os.path.exists(path)}
        n = set_shortcut_icons(valid) if valid else 0
        clear_pending_icons()  # reached a real write attempt while Steam was closed
        return n
    except Exception as e:
        print(f"Failed to apply pending icons: {e}"); return 0

def search_game(name, app_id=None):
    key = load_api_key(); headers = {"Authorization": f"Bearer {key}"}
    if app_id:
        o = load_name_overrides()
        if str(app_id) in o: name = o[str(app_id)]; print(f"  Using corrected name: {name}")
    clean = re.sub(r'\s*\(.*?\)', '', name).strip()
    short = ' '.join(clean.split()[:3])
    # dict.fromkeys preserves insertion order and deduplicates (e.g. short titles)
    for q in dict.fromkeys(q for q in [clean, short] if q):
        try:
            d = _sgdb_get(f"https://www.steamgriddb.com/api/v2/search/autocomplete/{_url_quote(q)}",
                          headers=headers, timeout=5).json()
            if d["success"] and d["data"]:
                g = d["data"][0]; print(f"  Found: {g['name']} (ID: {g['id']})"); return g["id"]
        except (requests.exceptions.RequestException, ValueError):
            return False  # transient network/parse error — caller should not skip permanently
        except Exception: _debug("search_game")
    print(f"  No results for: {clean}"); return None

def _pref_params(prefs):
    params = {}
    for key in ("nsfw", "humor"):
        v = prefs.get(key, "ok")
        params[key] = "false" if v == "never" else "true" if v == "must_have" else "any"
    v = prefs.get("animated", "never")
    if v == "never":       params["types"] = "static"
    elif v == "must_have": params["types"] = "animated"
    return params

def _get_styles(art_type, prefs, value):
    return [api for pref, api in _STYLE_MAP.get(art_type, {}).items() if prefs.get(pref) == value]

def _strip_never(results, art_type, prefs):
    never = set(_get_styles(art_type, prefs, "never"))
    return [r for r in results if r.get("style") not in never] if never else results

def get_artwork(sgdb_id, art_type, prefs=None, dimensions=None):
    if prefs is None:
        prefs = load_prefs()
    key = load_api_key()
    headers = {"Authorization": f"Bearer {key}"}
    url = f"https://www.steamgriddb.com/api/v2/{art_type}/game/{sgdb_id}"

    def fetch(params):
        p = dict(params)
        # Constrain to specific grid dimensions (portrait vs wide) when requested.
        if dimensions:
            p["dimensions"] = dimensions
        try:
            d = _sgdb_get(url, headers=headers, params=p, timeout=5).json()
            if d.get("success") and d.get("data"):
                return d["data"]
        except Exception:
            _debug("get_artwork fetch")
        return []

    animated_pref = prefs.get("animated", "never")
    nsfw_pref     = prefs.get("nsfw",     "ok")
    humor_pref    = prefs.get("humor",    "ok")
    must_styles   = _get_styles(art_type, prefs, "must_have")

    # Params that only block "never" preferences — "must_have" is treated as "any"
    def never_only_params():
        p = {
            "nsfw":  "false" if nsfw_pref  == "never" else "any",
            "humor": "false" if humor_pref == "never" else "any",
        }
        if animated_pref == "never":
            p["types"] = "static"
        return p

    # Ordered accumulator — deduplicates by URL, preserves insertion order
    collected, seen = [], set()

    def add(results):
        for r in _strip_never(results, art_type, prefs):
            if r["url"] not in seen:
                seen.add(r["url"])
                collected.append(r)

    must_have_count = (
        (1 if animated_pref == "must_have" else 0) +
        (1 if nsfw_pref     == "must_have" else 0) +
        (1 if humor_pref    == "must_have" else 0) +
        (1 if must_styles   else 0)
    )

    if must_have_count:
        # Tier 1: ALL "always" constraints together
        p1 = dict(_pref_params(prefs))
        if must_styles:
            p1["styles"] = ",".join(must_styles)
        add(fetch(p1))

        # Tier 2: individual "always" fetches — only if Tier 1 didn't give enough results
        if must_have_count > 1 and len(collected) < 5:
            if animated_pref == "must_have":
                p = dict(never_only_params()); p["types"] = "animated"
                add(fetch(p))
            if len(collected) < 5 and nsfw_pref == "must_have":
                p = dict(never_only_params()); p["nsfw"] = "true"
                add(fetch(p))
            if len(collected) < 5 and humor_pref == "must_have":
                p = dict(never_only_params()); p["humor"] = "true"
                add(fetch(p))
            if len(collected) < 5 and must_styles:
                p = dict(never_only_params()); p["styles"] = ",".join(must_styles)
                add(fetch(p))

    # Tier 3: popularity fill — only if we still need more results
    if len(collected) < 5:
        add(fetch(never_only_params()))

    if collected:
        return collected

    # Absolute fallback: no filters at all
    return fetch({})

def download_artwork(url, save_path, register=True):
    # Download to a temp file and only swap it into place once it's complete, so a
    # dropped connection can never leave a truncated image that gets applied/registered.
    tmp = save_path + ".part"
    try:
        r = _http_get(url, stream=True, timeout=10)
        if r.status_code != 200:
            print(f"    ❌ Failed ({r.status_code}): {url}"); return False
        written = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1024):
                f.write(chunk); written += len(chunk)
        expected = r.headers.get("Content-Length")
        if expected and expected.isdigit() and written != int(expected):
            raise IOError(f"incomplete download: {written}/{expected} bytes")
        os.replace(tmp, save_path)  # atomic within the same folder
        if register:
            register_managed_file(save_path)
        print(f"    ✅ Saved: {os.path.basename(save_path)}"); return True
    except Exception as e:
        _debug("download_artwork")
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except OSError: pass
        print(f"    ❌ Network error: {e}"); return False

def clear_slot_files(save_path, existing_files=None):
    """Delete any existing artwork sharing `save_path`'s grid slot but using a
    different extension. Steam keys art off the filename base (e.g. '12345p'), so a
    leftover '12345p.png' would keep displaying instead of a new '12345p.webp' —
    this guarantees the freshly downloaded file is the only one Steam sees.
    The previous file is already preserved by snapshot_for_restore() before this runs.

    existing_files: optional pre-listed folder snapshot to avoid a redundant listdir
    (download_all_artwork shares ONE snapshot across this game's slots — safe because
    each slot has a distinct filename base). Standalone callers leave it None."""
    folder = os.path.dirname(save_path)
    prefix = os.path.basename(save_path).rsplit(".", 1)[0] + "."
    if existing_files is None:
        try:
            existing_files = os.listdir(folder)
        except OSError:
            return
    for f in existing_files:
        if f.startswith(prefix):
            try: os.remove(os.path.join(folder, f))
            except OSError: pass

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

def download_apng(url, out_path, progress_cb=None, status_cb=None, register=True):
    """Download an animated webp/gif from `url` and write it to out_path as APNG.
    progress_cb(done, total) reports per-frame encode progress; status_cb(message)
    reports the current phase (download / read / convert). Returns True on success."""
    if status_cb:
        status_cb("Downloading…")
    src_ext = url.split(".")[-1].split("?")[0]
    tmp = f"{out_path}.src.{src_ext}"
    if not download_artwork(url, tmp, register=False):
        return False
    try:
        ok = convert_animated_to_apng(tmp, out_path, progress_cb, status_cb)
    except Exception as e:
        print(f"    ❌ APNG conversion failed: {e}"); ok = False
    finally:
        try: os.remove(tmp)
        except OSError: pass
    if ok and register:
        register_managed_file(out_path)
    return ok

# Bounded concurrency for SGDB requests. SGDB publishes no rate limit and throttles
# dynamically via HTTP 429 + Retry-After (handled by _sgdb_get's retry); a handful of
# workers gets the latency win without tripping that throttle.
SGDB_MAX_WORKERS = 5

def download_all_artwork(sgdb_id, unsigned_id, prefs=None, progress_cb=None, defer_icon=False):
    if prefs is None:
        prefs = load_prefs()
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
        existing_files = os.listdir(GRID_FOLDER)
    except OSError:
        existing_files = []
    snapshot_for_restore(unsigned_id, existing_files=existing_files)
    os.makedirs(CACHE_FOLDER, exist_ok=True)
    results = {}
    # When defer_icon=True, the icon's vdf write is deferred to the caller (batch write
    # at end of fetch-all). Surfaced via results["icon_to_set"] = (unsigned_id, path).
    results["icon_to_set"] = None
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
        return slot, get_artwork(sgdb_id, endpoint, prefs, dimensions=dimensions)

    with ThreadPoolExecutor(max_workers=SGDB_MAX_WORKERS) as ex:
        slot_images = dict(ex.map(fetch_slot, slots.items()))

    # --- Phase 2: per slot (declared order, so results/progress stay deterministic),
    # download the applied art then the previews (previews run concurrently) -----
    for slot, (art_label, endpoint, base, dimensions) in slots.items():
        images = slot_images.get(slot)
        if not images:
            # A missing slot still consumes its 1 main + 3 preview steps so the bar
            # reaches 100% exactly (same accounting as the serial version).
            for _ in range(4):
                bump(f"{art_label} — none found")
            results[slot] = None
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
        if applied_index is not None:
            top = options[applied_index]
            ext = top["url"].split(".")[-1].split("?")[0]
            save_path = os.path.join(GRID_FOLDER, f"{base}.{ext}")
            bump(f"Downloading {art_label}")
            clear_slot_files(save_path, existing_files=existing_files)
            download_artwork(top["url"], save_path)
            if slot == "icons":
                if defer_icon:
                    # Defer the shortcuts.vdf write so the caller can batch + write all
                    # icons in ONE atomic pass (and while Steam is closed).
                    results["icon_to_set"] = (unsigned_id, save_path)
                else:
                    set_shortcut_icon(unsigned_id, save_path)
        else:
            save_path = os.path.join(GRID_FOLDER, f"{base}.png")
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
            tp = os.path.join(CACHE_FOLDER, f"{base}_{i}.{e}")
            bump(f"Caching {art_label} preview {i+1}/3")
            download_artwork(u, tp, register=False)
            return i, tp

        if preview_imgs:
            with ThreadPoolExecutor(max_workers=SGDB_MAX_WORKERS) as ex:
                pairs = list(ex.map(fetch_preview, preview_imgs))
            # Re-sort by index so thumb_paths still lines up with options 0..2.
            thumbs = [tp for _i, tp in sorted(pairs, key=lambda p: p[0])]
        else:
            thumbs = []

        results[slot] = {"applied_path": save_path,
                              "applied_index": applied_index,
                              "option_urls": [img["url"] for img in options],
                              "option_meta": [
                                  {
                                      "animated": is_animated_image(img),
                                      "nsfw":     bool(img.get("nsfw", False)),
                                      "humor":    bool(img.get("humor", False)),
                                  }
                                  for img in options
                              ],
                              "thumb_paths": thumbs, "filename_base": base}
    return results

# --- Consuming a download_all_artwork() results dict ------------------------
# HAZARD: the results dict mixes two kinds of values. Art-slot keys
# ("grids"/"grids_wide"/"heroes"/"logos"/"icons") map to slot dicts (or None),
# but the reserved "icon_to_set" key maps to a TUPLE (unsigned_id, path) or None
# in defer_icon mode. A naive `for v in results.values(): v.get(...)` loop blows
# up with AttributeError on that tuple (tuples have no .get), and because the
# consumer runs in a daemon thread that crash silently hangs the UI on
# "Fetching…" forever. These helpers are the ONLY supported way to consume the
# dict: they iterate art slots and the icon entry separately, so the tuple can
# never reach .get(). Do NOT re-inline a values()-loop over results.
ICON_TO_SET_KEY = "icon_to_set"

def applied_paths_from_results(results):
    """Return the applied_path of every ART SLOT that auto-applied a static image
    (applied_index is not None and applied_path is set). Iterates only slot dicts
    and skips the reserved icon_to_set entry / any non-dict value, so the
    icon_to_set tuple can't trip a .get() call."""
    paths = []
    for key, art_data in results.items():
        if key == ICON_TO_SET_KEY or not isinstance(art_data, dict):
            continue
        if art_data.get("applied_index") is not None and art_data.get("applied_path"):
            paths.append(art_data["applied_path"])
    return paths

def icon_write_from_results(results):
    """Return the deferred icon write (unsigned_id, path) tuple, or None. This is
    the ONLY value in results that is a tuple rather than a slot dict."""
    return results.get(ICON_TO_SET_KEY)


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
            r = _http_get(_URL, headers=_HEADERS, timeout=10)
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
    key = load_api_key()
    if not key: print("❌ No API key set."); exit()
    print(f"API key: {key[:6]}...\n")
    clean_old_cache()
    games = get_non_steam_games()
    needs = [g for g in games if not g["has_art"]]
    print(f"Found {len(games)} games, {len(needs)} need artwork\n")
    for game in needs:
        print(f"🎮 {game['name']}")
        sid = search_game(game["name"], game["app_id"])
        if sid: download_all_artwork(sid, game["app_id"])
        else: print("  ⚠️ Not found")
        print()
    print("✅ Done! Restart Steam.")
