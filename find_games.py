import os, re, sys, platform, shutil, subprocess, time, json, threading, glob, tempfile, vdf, requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote as _url_quote
from datetime import datetime, timedelta, timezone
import appfiles

from appcommon import DEBUG, debug as _debug
import steam_env
import sgdb
import steam_files
import restore
import artwork


# --- Restore point (SPEC.md S3) --------------------------------------------------
# ~/.steamart_restore/ holds ONE restore point: manifest.json plus <id>/<file>
# byte copies (mtime preserved via copy2) taken before a fetch or Re-fetch
# destroys art, so Undo can put back what was there. The manifest is written
# atomically (temp file + os.replace), the same rule as _write_shortcuts_atomic
# and the download path. Every function here never raises (module contract).


# ---------------------------------------------------------------------------
# Window-placement helpers — pure arithmetic, no tkinter dependency.
# Extracted here so they can be unit-tested without a display.
# ---------------------------------------------------------------------------


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
    games = steam_files.get_non_steam_games()
    needs = [g for g in games if not g["has_art"]]
    print(f"Found {len(games)} games, {len(needs)} need artwork\n")
    for game in needs:
        print(f"🎮 {game['name']}")
        outcome, sid = sgdb.search_game(game["name"], game["app_id"])
        if outcome == sgdb.SEARCH_FOUND: artwork.download_all_artwork(sid, game["app_id"])
        else: print("  ⚠️ Not found")
        print()
    print("✅ Done! Restart Steam.")
