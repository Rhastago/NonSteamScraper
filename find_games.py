import os, re, sys, platform, shutil, subprocess, time, json, vdf, requests
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

def load_api_key():
    try:
        with open(APIKEY_FILE, encoding="utf-8") as f: return f.read().strip()
    except Exception: _debug("load_api_key"); return ""

def save_api_key(key):
    with open(APIKEY_FILE, "w", encoding="utf-8") as f: f.write(key.strip())

def verify_api_key(key):
    if not key or not key.strip(): return False
    try:
        r = requests.get("https://www.steamgriddb.com/api/v2/search/autocomplete/test",
                         headers={"Authorization": f"Bearer {key.strip()}"}, timeout=8)
        return r.status_code == 200 and bool(r.json().get("success"))
    except Exception: return False

def _sgdb_get(url, headers, params=None, timeout=5, retries=2):
    """requests.get wrapper that retries on HTTP 429 (rate-limit).

    Sleeps for the value of the Retry-After response header (parsed as int
    seconds), falling back to a short backoff (2 s, then 4 s), capped at 5 s
    per attempt.  After exhausting retries the final response is returned as-is
    so callers can inspect it normally.  Never raises for a 429."""
    backoffs = [2, 4]
    r = requests.get(url, headers=headers, params=params, timeout=timeout)
    for attempt in range(retries):
        if r.status_code != 429:
            break
        try:
            wait = min(int(r.headers.get("Retry-After", backoffs[attempt])), 5)
        except (ValueError, TypeError):
            wait = min(backoffs[attempt], 5)
        time.sleep(wait)
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
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

STEAM_USERDATA, STEAM_USER_ID = find_steam_user()
if STEAM_USER_ID is None:
    _udp = get_steam_path()
    if _udp and os.path.exists(_udp):
        _c = [u for u in os.listdir(_udp) if u != "0" and os.path.isdir(os.path.join(_udp, u))]
        if _c: STEAM_USERDATA, STEAM_USER_ID = _udp, _c[0]
STEAM_NOT_FOUND = STEAM_USER_ID is None
if STEAM_NOT_FOUND:
    SHORTCUTS_PATH = GRID_FOLDER = ""
else:
    SHORTCUTS_PATH = os.path.join(STEAM_USERDATA, STEAM_USER_ID, "config", "shortcuts.vdf")
    GRID_FOLDER    = os.path.join(STEAM_USERDATA, STEAM_USER_ID, "config", "grid")
    os.makedirs(GRID_FOLDER, exist_ok=True)

SKIP_FILE     = os.path.expanduser("~/.steamart_skip")
NAMES_FILE    = os.path.expanduser("~/.steamart_names")
CACHE_FOLDER  = os.path.expanduser("~/.steamart_cache")
BACKUP_FOLDER = os.path.expanduser("~/.steamart_backup")
MANAGED_FILE  = os.path.expanduser("~/.steamart_managed")
PREFS_FILE    = os.path.expanduser("~/.steamart_prefs")

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

def backup_artwork(unsigned_id):
    os.makedirs(BACKUP_FOLDER, exist_ok=True)
    managed = load_managed_files()
    for f in os.listdir(GRID_FOLDER):
        if f.startswith(str(unsigned_id)):
            src = os.path.join(GRID_FOLDER, f)
            if src in managed: shutil.copy2(src, os.path.join(BACKUP_FOLDER, f))

def restore_backup():
    if not os.path.exists(BACKUP_FOLDER): return False
    restored = 0
    for f in os.listdir(BACKUP_FOLDER):
        if f == "shortcuts.vdf.bak": continue
        shutil.copy2(os.path.join(BACKUP_FOLDER, f), os.path.join(GRID_FOLDER, f)); restored += 1
    return restored > 0

def clear_backup():
    if not os.path.exists(BACKUP_FOLDER): return
    for f in os.listdir(BACKUP_FOLDER):
        if f == "shortcuts.vdf.bak": continue
        p = os.path.join(BACKUP_FOLDER, f)
        if os.path.isfile(p): os.remove(p)

def full_reset():
    """Delete all app data — artwork, cache, prefs, API key, skip list, overrides."""
    clear_managed_artwork()
    clear_cache()
    clear_backup()
    for f in [APIKEY_FILE, PREFS_FILE, SKIP_FILE, NAMES_FILE, MANAGED_FILE,
              os.path.expanduser("~/.steamart_firstrun"),
              os.path.expanduser("~/.steamart_theme"),
              os.path.expanduser("~/.steamart_geometry")]:
        try:
            if os.path.exists(f): os.remove(f)
        except Exception:
            pass

def is_steam_running():
    if not PSUTIL_AVAILABLE: return False
    target = {"Linux": "steam", "Windows": "steam.exe", "Darwin": "steam"}.get(platform.system(), "steam").lower()
    try: return any(p.info["name"] and p.info["name"].lower() == target for p in psutil.process_iter(["name"]))
    except Exception: return False

def restart_steam():
    s = platform.system()
    try:
        if s == "Linux":   subprocess.Popen(["pkill", "-x", "steam"]); time.sleep(3); subprocess.Popen(["steam"])
        elif s == "Windows":
            install = _windows_steam_install() or r"C:\Program Files (x86)\Steam"
            subprocess.Popen(["taskkill", "/F", "/IM", "steam.exe"])
            time.sleep(3)
            subprocess.Popen([os.path.join(install, "steam.exe")])
        elif s == "Darwin":  subprocess.Popen(["pkill", "-x", "Steam"]); time.sleep(3); subprocess.Popen(["open", "-a", "Steam"])
    except Exception as e: print(f"Failed to restart Steam: {e}")

def get_non_steam_games():
    if not os.path.exists(SHORTCUTS_PATH): return []
    with open(SHORTCUTS_PATH, "rb") as f:
        data = vdf.binary_loads(f.read())
    shortcuts = data.get("shortcuts", {})
    skip_list = load_skip_list(); seen = set(); games = []
    for _key, game in shortcuts.items():
        app_id = game.get("appid")
        if app_id is None: continue
        name = game.get("AppName") or game.get("appname") or "unknown"
        uid = app_id & 0xFFFFFFFF
        if uid in seen: continue
        seen.add(uid)
        grid_files = [f for f in os.listdir(GRID_FOLDER) if f.startswith(str(uid))]
        in_skip = str(uid) in skip_list
        games.append({"name": name, "app_id": uid, "has_art": len(grid_files) > 0 or in_skip, "skipped": in_skip})
    return games

def set_shortcut_icon(unsigned_id, icon_path):
    """Write an icon path into shortcuts.vdf for the given app. Backs up on first write.
    Note: Steam should be closed when this runs to prevent it overwriting the change."""
    if not os.path.exists(SHORTCUTS_PATH): return False
    try:
        os.makedirs(BACKUP_FOLDER, exist_ok=True)
        bak = os.path.join(BACKUP_FOLDER, "shortcuts.vdf.bak")
        if not os.path.exists(bak): shutil.copy2(SHORTCUTS_PATH, bak)
        with open(SHORTCUTS_PATH, "rb") as f: data = vdf.binary_loads(f.read())
        changed = False
        # Update every shortcut sharing this appid, not just the first — duplicate
        # non-Steam entries with the same appid would otherwise miss the icon.
        for _key, game in data.get("shortcuts", {}).items():
            aid = game.get("appid")
            if aid is not None and (aid & 0xFFFFFFFF) == unsigned_id:
                game["icon"] = icon_path; changed = True
        if changed:
            with open(SHORTCUTS_PATH, "wb") as f: f.write(vdf.binary_dumps(data))
        return changed
    except Exception as e:
        print(f"Failed to set icon for {unsigned_id}: {e}"); return False

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
        r = requests.get(url, stream=True, timeout=10)
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

def clear_slot_files(save_path):
    """Delete any existing artwork sharing `save_path`'s grid slot but using a
    different extension. Steam keys art off the filename base (e.g. '12345p'), so a
    leftover '12345p.png' would keep displaying instead of a new '12345p.webp' —
    this guarantees the freshly downloaded file is the only one Steam sees.
    The previous file is already preserved by backup_artwork() before this runs."""
    folder = os.path.dirname(save_path)
    prefix = os.path.basename(save_path).rsplit(".", 1)[0] + "."
    try:
        existing = os.listdir(folder)
    except OSError:
        return
    for f in existing:
        if f.startswith(prefix):
            try: os.remove(os.path.join(folder, f))
            except OSError: pass

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

def download_all_artwork(sgdb_id, unsigned_id, prefs=None, progress_cb=None):
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
    backup_artwork(unsigned_id)
    os.makedirs(CACHE_FOLDER, exist_ok=True)
    results = {}
    # N slots × (1 main + 3 previews) steps max
    total_steps = len(slots) * 4
    step = 0
    for slot, (art_label, endpoint, base, dimensions) in slots.items():
        images = get_artwork(sgdb_id, endpoint, prefs, dimensions=dimensions)
        if not images:
            step += 4
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
            step += 1
            if progress_cb: progress_cb(f"Downloading {art_label}", step, total_steps)
            clear_slot_files(save_path)
            download_artwork(top["url"], save_path)
            if slot == "icons": set_shortcut_icon(unsigned_id, save_path)
            applied_url = top["url"]
        else:
            save_path = os.path.join(GRID_FOLDER, f"{base}.png")
            step += 1
            if progress_cb: progress_cb(f"{art_label} — animated, apply to commit", step, total_steps)
            applied_url = None

        thumbs = []
        for i, img in enumerate(options[:3]):
            u = img["url"]; e = u.split(".")[-1].split("?")[0]
            tp = os.path.join(CACHE_FOLDER, f"{base}_{i}.{e}")
            step += 1
            if progress_cb: progress_cb(f"Caching {art_label} preview {i+1}/3", step, total_steps)
            download_artwork(u, tp, register=False); thumbs.append(tp)
        results[slot] = {"applied_url": applied_url, "applied_path": save_path,
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
                              "thumb_paths": thumbs, "current_index": 0, "filename_base": base}
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
    nums = [int(n) for n in re.findall(r"\d+", after_eq)]
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
    final_w = max(1, min(desired_w, wa_w))
    final_h = max(1, min(desired_h, avail_h))
    # Centre horizontally; vertically leave reserve/2 of slack above and below
    # so neither the title bar nor the bottom buttons land under a panel/taskbar.
    x = wa_x + (wa_w - final_w) // 2
    y = wa_y + (reserve // 2) + (avail_h - final_h) // 2
    # Clamp every edge inside the work area.
    x = max(wa_x, min(x, wa_x + wa_w - final_w))
    y = max(wa_y, min(y, wa_y + wa_h - final_h))
    return final_w, final_h, x, y


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
