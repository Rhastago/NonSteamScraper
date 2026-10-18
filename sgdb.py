"""The app's HTTP client: the ONE outbound GET seam (http_get, a pooled
requests.Session shared by every worker thread), the SteamGridDB wrapper with
its 429 retry (_sgdb_get), the API-key check, the game search, the artwork
lookups with the art-style preferences, and the Steam store assets SGDB links
to. tkinter-free; tests patch http_get / _sgdb_get HERE."""

from requests.adapters import HTTPAdapter
from urllib.parse import quote as _url_quote
from datetime import datetime, timezone
import json
import re
import requests
import threading
import time
from appcommon import debug as _debug
import appfiles


# The app installs a callable taking one message string; find_games stays UI-free.
RATE_LIMIT_HOOK = None

# The three answers to "will SteamGridDB take this key?" (check_api_key). Only a
# refusal blames the key; "unreachable" means we could not ask.
KEY_ACCEPTED    = "accepted"
KEY_REJECTED    = "rejected"
KEY_UNREACHABLE = "unreachable"

def check_api_key(key=None):
    """Ask SteamGridDB whether `key` (default: the saved key) is accepted.
    Returns KEY_ACCEPTED (HTTP 200 with success true), KEY_REJECTED (HTTP 401 or
    403: SGDB refused the key itself, e.g. {"success":false,"errors":["Invalid key
    format"]}; also an empty key) or KEY_UNREACHABLE (anything else: no network,
    a timeout, a 5xx, a 429 that outlasted _sgdb_get's retries). Every fetch runs
    this before it changes anything on disk. Never raises."""
    if key is None:
        key = appfiles.load_api_key()
    if not key or not key.strip():
        return KEY_REJECTED
    try:
        r = _sgdb_get("https://www.steamgriddb.com/api/v2/search/autocomplete/test",
                      headers={"Authorization": f"Bearer {key.strip()}"}, timeout=8)
        if r.status_code in (401, 403):
            return KEY_REJECTED
        if r.status_code == 200 and r.json().get("success") is True:
            return KEY_ACCEPTED
    except Exception:
        _debug("check_api_key")
    return KEY_UNREACHABLE

def verify_api_key(key):
    """True only when SteamGridDB accepts `key` (the Settings Save check)."""
    if not key or not key.strip(): return False
    return check_api_key(key) == KEY_ACCEPTED

# One HTTP seam for the whole app: every outbound GET goes through http_get,
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

def http_get(url, headers=None, params=None, timeout=None, stream=False):
    """The single GET seam.  No retry logic here (that stays in _sgdb_get) and
    no exception swallowing — callers handle both."""
    return _get_session().get(url, headers=headers, params=params,
                              timeout=timeout, stream=stream)

def _sgdb_get(url, headers, params=None, timeout=5, retries=2):
    """http_get wrapper that retries on HTTP 429 (rate-limit).

    Sleeps for the value of the Retry-After response header (parsed as int
    seconds), falling back to a short backoff (2 s, then 4 s), capped at 5 s
    per attempt.  After exhausting retries the final response is returned as-is
    so callers can inspect it normally.  Never raises for a 429."""
    backoffs = [2, 4]
    r = http_get(url, headers=headers, params=params, timeout=timeout)
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
        r = http_get(url, headers=headers, params=params, timeout=timeout)
    return r

def _release_year(ts):
    """Convert a Unix timestamp to a 4-digit year string, or '' if absent."""
    try:
        return str(datetime.fromtimestamp(int(ts), tz=timezone.utc).year) if ts else ""
    except Exception:
        return ""

def search_sgdb_autocomplete(query):
    """Return list of {id, name} dicts from the SGDB autocomplete endpoint."""
    key = appfiles.load_api_key()
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

_STYLE_MAP = {
    "grids":  {"grid_no_logo": "no_logo", "grid_blurred": "blurred",
               "grid_alternate": "alternate", "grid_material": "material"},
    "heroes": {"hero_alternate": "alternateArt", "hero_blurred": "blurred"},
    "logos":  {"logo_official": "official", "logo_white": "white", "logo_black": "black"},
    "icons":  {"icon_official": "official"},
}

# search_game outcomes. Only SEARCH_NOT_FOUND may put a game on the skip list: a
# refusal or an error says nothing about the game (B3 — a revoked key used to hide
# the whole library, one game at a time).
SEARCH_FOUND     = "found"       # sgdb_id is the first autocomplete hit
SEARCH_NOT_FOUND = "not_found"   # every query got a genuine {"success": true, "data": []}
SEARCH_REFUSED   = "refused"     # HTTP 401/403: SGDB refused the API key
SEARCH_ERROR     = "error"       # network, timeout, 5xx, 429 after retries, bad body

def search_game(name, app_id=None):
    """Search SGDB for `name` (or the saved name override for app_id). Returns
    (outcome, sgdb_id); sgdb_id is set only when outcome is SEARCH_FOUND."""
    key = appfiles.load_api_key(); headers = {"Authorization": f"Bearer {key}"}
    if app_id:
        o = appfiles.load_name_overrides()
        if str(app_id) in o: name = o[str(app_id)]; print(f"  Using corrected name: {name}")
    clean = re.sub(r'\s*\(.*?\)', '', name).strip()
    short = ' '.join(clean.split()[:3])
    # dict.fromkeys preserves insertion order and deduplicates (e.g. short titles)
    for q in dict.fromkeys(q for q in [clean, short] if q):
        try:
            r = _sgdb_get(f"https://www.steamgriddb.com/api/v2/search/autocomplete/{_url_quote(q)}",
                          headers=headers, timeout=5)
            if r.status_code in (401, 403):
                return SEARCH_REFUSED, None
            if r.status_code != 200:
                return SEARCH_ERROR, None
            d = r.json()
            if not isinstance(d, dict) or d.get("success") is not True \
                    or not isinstance(d.get("data"), list):
                return SEARCH_ERROR, None
            if d["data"]:
                g = d["data"][0]; print(f"  Found: {g['name']} (ID: {g['id']})")
                return SEARCH_FOUND, g["id"]
        except Exception:
            _debug("search_game")
            return SEARCH_ERROR, None  # transient — the caller must not skip permanently
    print(f"  No results for: {clean}"); return SEARCH_NOT_FOUND, None

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
        prefs = appfiles.load_prefs()
    key = appfiles.load_api_key()
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

# Bounded concurrency for SGDB requests. SGDB publishes no rate limit and throttles
# dynamically via HTTP 429 + Retry-After (handled by _sgdb_get's retry); a handful of
# workers gets the latency win without tripping that throttle.
SGDB_MAX_WORKERS = 5

# --- Official Steam store assets ----------------------------------------------
# SGDB links a game to its Steam app and serves that app's asset metadata via
# GET /games/id/{id}?platformdata=steam -> data.external_platform_data.steam[0]
# = {"id": "<steam appid>", "metadata": {...}}. The URL templates below are the
# ones SGDB's own game page ("Steam" tab) builds from that metadata. Filenames may
# carry a hash folder on newer apps ("<hash>/library_capsule_2x.jpg") — they're
# joined verbatim, so both layouts work.
STEAM_STORE_CDN = "https://shared.steamstatic.com/store_item_assets/steam/apps"
STEAM_COMMUNITY_CDN = "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps"

def get_steam_platform_data(sgdb_id):
    """Return (steam_appid, metadata) for the Steam app SGDB links to `sgdb_id`, or
    None when there's no Steam link or the request fails. Never raises — a miss just
    means the fetch proceeds with SGDB art only."""
    try:
        headers = {"Authorization": f"Bearer {appfiles.load_api_key()}"}
        d = _sgdb_get(f"https://www.steamgriddb.com/api/v2/games/id/{sgdb_id}",
                      headers=headers, params={"platformdata": "steam"}, timeout=5).json()
        entries = ((d.get("data") or {}).get("external_platform_data") or {}).get("steam") or []
        if entries and entries[0].get("id") and isinstance(entries[0].get("metadata"), dict):
            return str(entries[0]["id"]), entries[0]["metadata"]
    except Exception:
        _debug("get_steam_platform_data")
    return None

def _steam_lang_pick(by_lang):
    """From a {language: filename} dict pick English, else the first listed."""
    if not isinstance(by_lang, dict) or not by_lang:
        return None
    return by_lang.get("english") or next(iter(by_lang.values()), None)

def steam_official_images(steam_appid, metadata):
    """Map Steam store metadata to {slot: image_record} for every slot Steam has art
    for. Records look like SGDB image records (url/mime/style/nsfw/humor) plus
    "steam_official": True, so download_all_artwork can put them first unchanged.
    style is "official" so a Logo/Icon "Official = Exclude" pref drops them via
    _strip_never. Pure — no network.

    Size picks: cover 2x (600x900, Steam's grid size), hero 1x (1920 wide; the 2x is
    3840), logo 2x, header 1x (Steam serves no header_2x), icon = the 256px
    clienticon .ico (the .jpg icon is 32x32), falling back to the .jpg."""
    mtime = metadata.get("store_asset_mtime")
    suffix = f"?t={mtime}" if mtime else ""

    def store(field, prefer):
        full = metadata.get(field) or {}
        if field == "header_image_full":
            fname = _steam_lang_pick(full)
        else:
            order = ("image2x", "image") if prefer == "2x" else ("image", "image2x")
            fname = next((f for f in (_steam_lang_pick(full.get(k)) for k in order) if f), None)
        if not fname:
            return None
        return f"{STEAM_STORE_CDN}/{steam_appid}/{fname}{suffix}"

    urls = {
        "grids":      store("library_capsule_full", "2x"),
        "grids_wide": store("header_image_full", "1x"),
        "heroes":     store("library_hero_full", "1x"),
        "logos":      store("library_logo_full", "2x"),
    }
    if metadata.get("clienticon"):
        urls["icons"] = f"{STEAM_COMMUNITY_CDN}/{steam_appid}/{metadata['clienticon']}.ico"
    elif metadata.get("icon"):
        urls["icons"] = f"{STEAM_COMMUNITY_CDN}/{steam_appid}/{metadata['icon']}.jpg"

    mimes = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "ico": "image/x-icon"}
    out = {}
    for slot, url in urls.items():
        if not url:
            continue
        ext = url.split(".")[-1].split("?")[0].lower()
        out[slot] = {"url": url, "mime": mimes.get(ext, "image/" + ext), "style": "official",
                     "nsfw": False, "humor": False, "steam_official": True}
    return out
