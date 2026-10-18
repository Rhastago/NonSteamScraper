# NonSteamScraper

Automatically fetch and apply cover art, hero images, logos, and icons for your non-Steam game shortcuts — directly from [SteamGridDB](https://www.steamgriddb.com).

Built for Steam Deck and Windows. No fuss, no subscriptions — just bring your own free API key.

---

## Download

Go to [Releases](https://github.com/Rhastago/NonSteamScraper/releases) and download the binary for your platform:

- **Linux / Steam Deck** — `NonSteamScraper-linux`
- **Windows** — `NonSteamScraper-windows.exe`

No Python installation required. Just download and run.

> **Windows note:** On first launch Windows may show a SmartScreen warning ("Windows protected your PC"). This is expected for unsigned freeware — click **More info → Run anyway** to proceed.

---

## What it does

- Detects all non-Steam game shortcuts in your Steam library
- Searches SteamGridDB for matching artwork
- Downloads and applies:
  - **Cover** (portrait grid image)
  - **Wide Cover** (landscape grid image, used in Big Picture and the old grid view)
  - **Hero** (wide background banner)
  - **Logo** (transparent game logo overlay)
  - **Icon** (shortcut icon, written directly into Steam's shortcuts file)
- Shows a results screen after fetching so you can review and swap any art you don't like
- Lets you cycle through up to 5 alternatives per art type and apply whichever you prefer

---

## Getting started

### 1. Get a free SteamGridDB API key

Create a free account at [steamgriddb.com](https://www.steamgriddb.com), then go to:

**Profile → Preferences → API** and generate a key.

### 2. Add your games to Steam

In Steam: **Games → Add a Non-Steam Game to My Library**

Add every shortcut you want artwork for before opening this app.

### 3. Run the app and add your key

Open NonSteamScraper, click ⚙ Settings, paste your key, and hit **Save**. The app verifies the key against SteamGridDB before saving — you'll see a green ✓ confirmation.

### 4. Fetch artwork

Click **Fetch Missing Artwork**. The app searches and downloads art for every game that doesn't have it yet. When it finishes, the results screen opens automatically.

### 5. Review and swap

On the results screen each game shows its Cover, Wide Cover, Hero, Logo, and Icon rows. Use ◀ ▶ to cycle through alternatives and **Apply this one** to swap. Click the image to open the full-size version in your browser.

### 6. Restart Steam

Art changes take effect after Steam restarts. The app will remind you.

---

## Features

| Feature | Details |
|---|---|
| Art types | Cover, Wide Cover, Hero, Logo, Icon |
| Alternatives | Up to 5 per art type, swappable after fetch |
| Official Steam art | When the game is on the Steam store, its own Steam artwork is the first (applied) option, tagged STEAM |
| Icon support | Written into `shortcuts.vdf` so Steam actually reads it |
| Undo | Restores the previous fetch's artwork in one click |
| Re-fetch | Reset a single game's art and fetch fresh results |
| SGDB Search | Live search dialog with autocomplete — find the right match if auto-search misses it |
| Art Style Preferences | Per-type filters for animated art, NSFW, humor, style variants, and more |
| Hidden list | Games not found on SGDB, and games you **Ignore**, are hidden; **Restore** brings one back, Factory reset clears all |
| Multi-account | Detects multiple Steam accounts and lets you switch between them |
| Refresh | Reload the library mid-session after adding new shortcuts |
| Cache | Thumbnails cached locally; auto-cleared after 30 days |
| Themes | Dark (default) and light mode, plus four accent colors (blue / green / purple / teal) |
| Window persistence | Remembers window position between sessions |
| Resizable | All windows resize and scroll |
| Factory reset | One-click reset of settings to first-launch state (your fetched artwork is preserved) |
| Cross-platform | Linux (Steam Deck) and Windows are supported. macOS path code exists but is **untested and unsupported** — no macOS builds are shipped. |

---

## Art Style Preferences

Click the 🎨 button to open Art Style Preferences. Every filter — both content and visual style — uses the same three states:

| State | Meaning |
|---|---|
| **Prefer** | Try this first |
| **Allow** | Include normally (the neutral default) |
| **Exclude** | Never use this |

You won't get zero results: "Prefer" is a soft preference — the app tries your preferred content/styles first and **automatically broadens** the search if too few matches are found (only "Exclude" is a hard filter). Hover any row for a short explanation.

**Content filters:**

- **Animated** — animated artwork (note: animated covers are not auto-applied; pick one in the results screen and click **Apply this one** to convert it to APNG — the only animated format Steam renders — with a progress popup. Files are large and conversion takes a while.)
- **NSFW** — explicit adult content (confirmation required on first enable per session)
- **Humor / Memes** — meme-style artwork

**Style filters (per art type):**

- **Cover:** No logo overlay, Alternate style, Blurred, Material design
- **Hero:** Alternate art, Blurred
- **Logo:** Official, White style, Black style
- **Icon:** Official

---

## Settings

| Setting | What it does |
|---|---|
| API Key | Paste and verify your SteamGridDB key |
| Art Style Preferences | Fine-tune what kinds of artwork are fetched |
| Appearance | Toggle dark / light mode |
| Steam Account | Switch between accounts if you have more than one |
| Cache | View cache size and clear thumbnails |
| Steam | See if Steam is running; restart it from the app |
| Clear All Artwork | Remove only art added by this app — manual art is untouched |
| Factory Reset | Reset settings to first-launch state (key, prefs, cache). Your fetched artwork is kept — use **Clear All Artwork** for that |

---

## Notes on icons

Steam reads non-Steam game icons from `shortcuts.vdf`, not from the grid folder. When NonSteamScraper applies an icon it writes the file path directly into that file. For this to stick:

**Close Steam before fetching or applying icons.** If Steam is running when the icon is applied, Steam may overwrite `shortcuts.vdf` on exit and lose the change.

A backup of each Steam account's `shortcuts.vdf` is saved to `~/.steamart_backup/shortcuts.vdf.<account id>.bak` before the app first writes to it. (Versions before 1.5.2 kept a single `shortcuts.vdf.bak` for the first account only; it is left in place.)

---

## Data stored locally

All state is stored in your home directory:

| File | Purpose |
|---|---|
| `~/.steamart_apikey` | Your SteamGridDB API key |
| `~/.steamart_prefs` | Art style preferences |
| `~/.steamart_theme` | Dark or light mode preference |
| `~/.steamart_accent` | Accent colour |
| `~/.steamart_account` | The Steam account you picked (when there is more than one) |
| `~/.steamart_firstrun` | Marks that the Quick Start guide has been shown |
| `~/.steamart_skip` | Hidden games — not found on SGDB, or ignored by you |
| `~/.steamart_names` | Custom search name overrides per game |
| `~/.steamart_ids` | The SteamGridDB match you picked with Search, per game |
| `~/.steamart_managed` | Tracks which files this app created (for safe cleanup) |
| `~/.steamart_pending_icons_<account id>` | Icons waiting for Steam to close before they are written |
| `~/.steamart_cache/` | Thumbnail cache |
| `~/.steamart_restore/` | The Undo restore point: copies of the art your last fetch or Re-fetch replaced |
| `~/.steamart_backup/` | Backup of each account's shortcuts.vdf, and artwork copies left by versions before 1.5.0 |
| `~/.steamart_geometry` | Window position saved by older versions (no longer written; Reset removes it) |

Reset App to Factory Defaults removes the settings files and the cache; it keeps your artwork, `~/.steamart_managed`, `~/.steamart_restore/` and `~/.steamart_backup/`.

---

## Building from source

Requires Python 3.12 or newer and pip (the release builds use 3.12; development uses 3.13).

```bash
git clone https://github.com/Rhastago/NonSteamScraper.git
cd NonSteamScraper
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

Tests: `pip install -r requirements-dev.txt`, then `python -m pytest -q`.

### Building a standalone executable

Install the build tool once (`pip install -r requirements-build.txt`), then run the
helper script for your platform. The scripts hold the exact PyInstaller flags; the
two flag lists are kept identical.

**Linux (run from a native terminal — not the VSCode integrated terminal):**
```bash
./build_linux.sh
```

**Windows (run in Git Bash):**
```bash
./build_windows.sh
```

The executable will be in the `dist/` folder.

### Automated release builds

Pushing a version tag (e.g. `git push origin v1.1.0`) triggers the
[release workflow](.github/workflows/release.yml), which builds the Linux and Windows
binaries in a clean environment and attaches them to the matching GitHub Release.
(Requires the repo's *Settings → Actions → Workflow permissions* to allow read/write.)

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history. Latest: **v1.5.2** — a safety release: Search → Select no longer deletes another game's art and fetches the exact game you picked, a rejected API key no longer hides your library, a failed download keeps your old art, nothing that changes your files can run during a fetch, and each Steam account gets its own `shortcuts.vdf` backup.

---

## License

MIT

UI icons are from [Microsoft Fluent Emoji](https://github.com/microsoft/fluentui-emoji), also MIT licensed.

---

*Built with Python, tkinter, and the SteamGridDB API.*
