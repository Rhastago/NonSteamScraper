# NonSteamScraper

Automatically fetch and apply cover art, hero images, logos, and icons for your non-Steam game shortcuts — directly from [SteamGridDB](https://www.steamgriddb.com).

Built for Steam Deck and Windows. No fuss, no subscriptions — just bring your own free API key.

---

## Download

Go to [Releases](https://github.com/Rhastago/NonSteamScraperPOC/releases) and download the binary for your platform:

- **Linux / Steam Deck** — `NonSteamScraper`
- **Windows** — `NonSteamScraper.exe`

No Python installation required. Just download and run.

---

## What it does

- Detects all non-Steam game shortcuts in your Steam library
- Searches SteamGridDB for matching artwork
- Downloads and applies:
  - **Cover** (portrait grid image)
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

Open NonSteamScraper, click ⚙ Settings, paste your key, and hit **Save**. The app verifies the key against SteamGridDB before saving it — you'll see a green ✓ confirmation.

### 4. Fetch artwork

Click **Fetch Missing Artwork**. The app searches and downloads art for every game that doesn't have it yet. When it finishes, the results screen opens automatically.

### 5. Review and swap

On the results screen each game shows its Cover, Hero, Logo, and Icon rows. Use ◀ ▶ to cycle through alternatives and **Apply this one** to swap. Click the image to open the full-size version in your browser.

### 6. Restart Steam

Art changes take effect after Steam restarts. The app will remind you.

---

## Features

| Feature | Details |
|---|---|
| Art types | Cover, Hero, Logo, Icon |
| Alternatives | Up to 5 per art type, fetched on demand |
| Icon support | Written into `shortcuts.vdf` so Steam actually reads it |
| Undo | Restores the previous fetch's artwork in one click |
| Re-fetch | Reset a single game's art and fetch fresh results |
| Rename | Override a game's search name if SteamGridDB can't find it |
| Skip list | Games not found are hidden; reset them any time |
| Refresh | Reload the library mid-session after adding new shortcuts |
| Cache | Thumbnails cached locally; auto-cleared after 30 days |
| Themes | Dark mode (default) and light mode |
| Resizable | All windows resize and scroll |
| Cross-platform | Linux, Windows (macOS path support included, untested) |

---

## Notes on icons

Steam reads non-Steam game icons from `shortcuts.vdf`, not from the grid folder. When NonSteamScraper applies an icon it writes the file path directly into that file. For this to stick:

**Close Steam before fetching or applying icons.** If Steam is running when the icon is applied, Steam may overwrite `shortcuts.vdf` on exit and lose the change.

A backup of your original `shortcuts.vdf` is saved to `~/.steamart_backup/shortcuts.vdf.bak` before the first write.

---

## Settings

| Setting | What it does |
|---|---|
| API Key | Paste and verify your SteamGridDB key |
| Appearance | Toggle dark / light mode |
| Steam Account | Switch between accounts if you have more than one |
| Cache | View cache size and clear thumbnails |
| Steam | See if Steam is running; restart it from the app |
| Clear All Artwork | Remove only art added by this app — manual art is untouched |

---

## Data stored locally

All state is stored in your home directory:

| File | Purpose |
|---|---|
| `~/.steamart_apikey` | Your SteamGridDB API key |
| `~/.steamart_theme` | Dark or light mode preference |
| `~/.steamart_skip` | Games permanently skipped (not found on SGDB) |
| `~/.steamart_names` | Your corrected search names per game |
| `~/.steamart_managed` | Tracks which files this app created (for safe cleanup) |
| `~/.steamart_cache/` | Thumbnail cache |
| `~/.steamart_backup/` | Backup of previous artwork and shortcuts.vdf |

---

## Building from source

Requires Python 3.10+ and pip.

```bash
git clone https://github.com/Rhastago/NonSteamScraperPOC.git
cd NonSteamScraperPOC
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install requests vdf Pillow psutil
python3 app.py
```

### Building a standalone executable

**Linux:**
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name NonSteamScraper \
  --add-data "icon.png:." \
  --hidden-import PIL._tkinter_finder \
  --hidden-import PIL.PngImagePlugin --hidden-import PIL.JpegImagePlugin \
  --hidden-import PIL.WebPImagePlugin --hidden-import PIL.GifImagePlugin \
  --hidden-import PIL.IcoImagePlugin --collect-all pillow app.py
```

**Windows** (run in PowerShell):
```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name NonSteamScraper --icon icon.ico `
  --add-data "icon.png;." `
  --hidden-import PIL._tkinter_finder `
  --hidden-import PIL.PngImagePlugin --hidden-import PIL.JpegImagePlugin `
  --hidden-import PIL.WebPImagePlugin --hidden-import PIL.GifImagePlugin `
  --hidden-import PIL.IcoImagePlugin --hidden-import requests `
  --hidden-import vdf --hidden-import psutil --collect-all pillow app.py
```

The executable will be in the `dist/` folder.

---

## License

No license yet. Planning MIT for v1.0.

---

*Built with Python, tkinter, and the SteamGridDB API.*
