"""What is left of the old god module after T-A5 split it into appfiles,
steam_env, sgdb, steam_files, restore, artwork, winplace and updates: the
original command-line fetch. Nothing imports this file."""

import appfiles
import artwork
import sgdb
import steam_env
import steam_files


if __name__ == "__main__":
    steam_env.init()
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
