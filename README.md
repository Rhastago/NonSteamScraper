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
| Icon support | Written into `shortcuts.vdf` so Steam actually reads it |
| Undo | Restores the previous fetch's artwork in one click |
| Re-fetch | Reset a single game's art and fetch fresh results |
| SGDB Search | Live search dialog with autocomplete — find the right match if auto-search misses it |
| Art Style Preferences | Per-type filters for animated art, NSFW, humor, style variants, and more |
| Skip list | Games not found on SGDB are hidden; reset individually or all at once |
| Multi-account | Detects multiple Steam accounts and lets you switch between them |
| Refresh | Reload the library mid-session after adding new shortcuts |
| Cache | Thumbnails cached locally; auto-cleared after 30 days |
| Themes | Dark mode (default) and light mode |
| Window persistence | Remembers window position between sessions |
| Resizable | All windows resize and scroll |
| Factory reset | One-click option to wipe all settings and start fresh |
| Cross-platform | Linux (Steam Deck) and Windows are supported. macOS path code exists but is **untested and unsupported** — no macOS builds are shipped. |

---

## Art Style Preferences

Click the 🎨 button to open Art Style Preferences. Each option has three states:

| State | Meaning |
|---|---|
| **Always** | Only fetch artwork matching this filter |
| **OK** | Include this type if other results are scarce |
| **Never** | Exclude this type entirely |

Available filters:

- **Animated** — animated artwork (note: animated covers are not auto-applied; pick one in the results screen and click **Apply this one** to convert it to APNG — the only animated format Steam renders — with a progress popup. Files are large and conversion takes a while.)
- **NSFW** — explicit adult content (confirmation required on first enable per session)
- **Humor / Memes** — meme-style artwork
- **Cover:** No logo overlay, Alternate style, Blurred, Material design
- **Hero:** Alternate art, Blurred

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
| Factory Reset | Wipe everything (key, art, prefs, cache) and restart as first launch |

---

## Notes on icons

Steam reads non-Steam game icons from `shortcuts.vdf`, not from the grid folder. When NonSteamScraper applies an icon it writes the file path directly into that file. For this to stick:

**Close Steam before fetching or applying icons.** If Steam is running when the icon is applied, Steam may overwrite `shortcuts.vdf` on exit and lose the change.

A backup of your original `shortcuts.vdf` is saved to `~/.steamart_backup/shortcuts.vdf.bak` before the first write.

---

## Data stored locally

All state is stored in your home directory:

| File | Purpose |
|---|---|
| `~/.steamart_apikey` | Your SteamGridDB API key |
| `~/.steamart_prefs` | Art style preferences |
| `~/.steamart_theme` | Dark or light mode preference |
| `~/.steamart_geometry` | Saved window position |
| `~/.steamart_skip` | Games permanently skipped (not found on SGDB) |
| `~/.steamart_names` | Custom search name overrides per game |
| `~/.steamart_managed` | Tracks which files this app created (for safe cleanup) |
| `~/.steamart_cache/` | Thumbnail cache |
| `~/.steamart_backup/` | Backup of previous artwork and shortcuts.vdf |

---

## Building from source

Requires Python 3.10+ and pip.

```bash
git clone https://github.com/Rhastago/NonSteamScraper.git
cd NonSteamScraper
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

### Building a standalone executable

**Linux (run from a native terminal — not the VSCode integrated terminal):**
```bash
pyinstaller --onefile --windowed --name NonSteamScraper \
  --add-data "icon.png:." \
  --add-data "assets:assets" \
  --hidden-import PIL._tkinter_finder \
  --hidden-import PIL.PngImagePlugin --hidden-import PIL.JpegImagePlugin \
  --hidden-import PIL.WebPImagePlugin --hidden-import PIL.GifImagePlugin \
  --hidden-import PIL.IcoImagePlugin --collect-all pillow app.py
```

**Windows (run in Git Bash or PowerShell):**
```bash
pyinstaller --onefile --windowed --name NonSteamScraper --icon icon.ico \
  --add-data "icon.png;." \
  --add-data "assets;assets" \
  --hidden-import PIL._tkinter_finder \
  --hidden-import PIL.PngImagePlugin --hidden-import PIL.JpegImagePlugin \
  --hidden-import PIL.WebPImagePlugin --hidden-import PIL.GifImagePlugin \
  --hidden-import PIL.IcoImagePlugin --hidden-import requests \
  --hidden-import vdf --hidden-import psutil \
  --hidden-import _socket --hidden-import socket \
  --collect-all pillow --collect-all multiprocessing app.py
```

The executable will be in the `dist/` folder.

You can also run the helper scripts directly: `./build_linux.sh` or `./build_windows.sh`.

### Automated release builds

Pushing a version tag (e.g. `git push origin v1.1.0`) triggers the
[release workflow](.github/workflows/release.yml), which builds the Linux and Windows
binaries in a clean environment and attaches them to the matching GitHub Release.
(Requires the repo's *Settings → Actions → Workflow permissions* to allow read/write.)

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history. Latest: **v1.3.0** — multiple Steam accounts, in-app update check, and deferred auto-apply of icons (queued while Steam is open, applied when it closes).

---

## License

MIT

UI icons are from [Microsoft Fluent Emoji](https://github.com/microsoft/fluentui-emoji), also MIT licensed.

---

*Built with Python, tkinter, and the SteamGridDB API.*
