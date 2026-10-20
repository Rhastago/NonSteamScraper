# NonSteamScraper

Give your non-Steam games the same polished look as the rest of your Steam library.

NonSteamScraper finds the games you added to Steam yourself (emulators, other launchers,
DRM-free installs) and fills in their missing artwork from
[SteamGridDB](https://www.steamgriddb.com): covers, banners, logos and icons, in one click.

Made for the **Steam Deck** (and Linux in general) and **Windows**. Free, no account with us, just a
free SteamGridDB API key.

---

## Download

Grab the latest build from [Releases](https://github.com/Rhastago/NonSteamScraper/releases):

- **Linux / Steam Deck**: `NonSteamScraper-linux`
- **Windows**: `NonSteamScraper-windows.exe`

Nothing to install. Download it and run it.

> **Windows:** the first launch may show "Windows protected your PC". That's normal for unsigned
> freeware. Click **More info → Run anyway**.

---

## Quick start

1. **Get an API key.** Make a free account at [steamgriddb.com](https://www.steamgriddb.com), then
   go to **Profile → Preferences → API** and generate a key.
2. **Add your games to Steam** with **Games → Add a Non-Steam Game to My Library**.
3. **Open NonSteamScraper**, click ⚙ Settings, paste the key and save. The app checks the key
   before keeping it.
4. **Click Fetch Missing Artwork.** Every game without art gets some.
5. **Review the results.** Don't like a pick? Flip through the alternatives with ◀ ▶ and choose
   **Apply this one**.
6. **Restart Steam** to see the new look.

---

## What you get

**Five kinds of artwork per game:**

| Art | Where Steam shows it |
|---|---|
| Cover | The portrait tile in your library |
| Wide Cover | The landscape tile (Big Picture and the older grid view) |
| Hero | The big banner at the top of the game's page |
| Logo | The game's title, laid over the hero |
| Icon | The small icon next to the game's name |

**And around it:**

- **Choose instead of accept.** Up to 5 alternatives for each art type, swappable after the fetch.
  Click any image to see it full size.
- **Official art first.** If the game is also on the Steam store, its own Steam artwork is offered
  first, tagged STEAM.
- **Wrong game matched?** Use **Search** to find the right one on SteamGridDB, or **Re-fetch** to
  start a single game over.
- **Undo.** One click restores the artwork your last fetch replaced.
- **Hide what you don't need.** **Ignore** a game (tools, launchers) and it stays out of the
  way. **Restore** brings it back.
- **Several Steam accounts?** Switch between them in Settings.
- **Dark or light theme**, with four accent colours.

---

## Art style preferences

The 🎨 button decides what kind of artwork you'd like. Each option can be set to:

- **Prefer**: try this first
- **Allow**: no preference (the default)
- **Exclude**: never use this

"Prefer" is a wish, not a filter: if there's too little preferred art, the app widens the search
so you still get something. Only "Exclude" is strict.

You can steer **animated**, **NSFW** and **humor** artwork, plus style options per art type
(blurred, alternate, white or black logos and more).

> **Animated art** is never applied automatically. Pick one in the results and the app converts it
> to APNG, the only animated format Steam plays. The files are big and conversion takes a moment.

---

## Good to know

- **Icons and a running Steam.** Steam keeps icons in its `shortcuts.vdf` file and overwrites it
  when it exits. If Steam is open, the app queues your icons and writes them as soon as Steam
  closes. The results screen also has a **Close Steam & Apply Icons** button.
- **Your own artwork is safe.** The app keeps track of the files it added. **Clear All Artwork**
  removes only those and leaves art you set by hand alone.
- **Backups.** Before it first changes `shortcuts.vdf`, the app saves a copy of it for each Steam
  account.
- **Where settings live.** Small `~/.steamart_*` files in your home folder. **Reset App to Factory
  Defaults** clears them but keeps your artwork.

---

## Building from source

Needs Python 3.12 or newer.

```bash
git clone https://github.com/Rhastago/NonSteamScraper.git
cd NonSteamScraper
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

Standalone builds: install `requirements-build.txt`, then run `./build_linux.sh` or
`./build_windows.sh` (Git Bash). Output goes to `dist/`.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. UI icons are from [Microsoft Fluent Emoji](https://github.com/microsoft/fluentui-emoji), also
MIT.
