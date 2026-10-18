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
