"""Where Steam is and who is logged in: the userdata folder, the active
account and its two paths (shortcuts.vdf and the grid folder), the account
switch, and the Steam process itself (running? stop / start / restart).
tkinter-free.

The account globals below are EMPTY until init() runs: app.py calls it once at
startup, before any window. (They used to be discovered at import time, so
merely importing the module scanned for Steam and created folders.)

Read them live — steam_env.GRID_FOLDER at call time, never
`from steam_env import GRID_FOLDER`: set_active_user rebinds them, and a
by-value copy keeps writing into the old account's folders."""

import os
import platform
import subprocess
import time
import vdf
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from appcommon import debug as _debug
import appfiles


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

# The active account. Set by init() and set_active_user(); empty until then.
STEAM_USERDATA = STEAM_USER_ID = None
STEAM_NOT_FOUND = True
SHORTCUTS_PATH = GRID_FOLDER = ""

def init():
    """Find Steam's userdata folder and pick the account: the first one with a
    shortcuts.vdf (else any account folder), replaced by the account saved in
    ~/.steamart_account when that one is still valid — which only matters with
    2+ accounts. Binds the account globals and creates the account's grid
    folder. Called once by app.py at startup."""
    global STEAM_USERDATA, STEAM_USER_ID, SHORTCUTS_PATH, GRID_FOLDER, STEAM_NOT_FOUND
    userdata, user_id = find_steam_user()
    if user_id is None:
        udp = get_steam_path()
        if udp and os.path.exists(udp):
            found = [u for u in os.listdir(udp) if u != "0" and os.path.isdir(os.path.join(udp, u))]
            if found: userdata, user_id = udp, found[0]
    if user_id is not None:
        try:
            with open(appfiles.ACCOUNT_FILE, encoding="utf-8") as f:
                saved = f.read().strip()
            udp = get_steam_path()
            if saved and udp and os.path.exists(os.path.join(udp, saved, "config", "shortcuts.vdf")):
                userdata, user_id = udp, saved
        except Exception:
            _debug("restore_active_user")
    STEAM_USERDATA, STEAM_USER_ID = userdata, user_id
    STEAM_NOT_FOUND = user_id is None
    if STEAM_NOT_FOUND:
        SHORTCUTS_PATH = GRID_FOLDER = ""
    else:
        SHORTCUTS_PATH = os.path.join(userdata, user_id, "config", "shortcuts.vdf")
        GRID_FOLDER    = os.path.join(userdata, user_id, "config", "grid")
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
        appfiles.write_text_atomic(appfiles.ACCOUNT_FILE, user_id)
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

# ---------------------------------------------------------------------------
# is_steam_running TTL cache
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
