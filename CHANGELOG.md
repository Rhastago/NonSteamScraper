# Changelog

All notable changes to NonSteamScraper are documented here.

This project adheres to [Semantic Versioning](https://semver.org).

## [1.2.5] — 2026-06-26

### Added
- **Search / filter box on the main window** — type to instantly filter the game
  list (and the Hidden section) by name; Escape or the ✕ button clears it. Makes
  large libraries manageable. The filter is debounced so big libraries stay smooth.

### Changed
- **Faster artwork fetching.** Downloads now run with bounded concurrency (a small
  thread pool) instead of strictly one-at-a-time, so fetching a library is
  noticeably quicker. Concurrency is kept conservative and still honors
  SteamGridDB's dynamic rate-limiting (HTTP 429 + Retry-After).
- Library scanning is faster and more accurate — the grid folder is read once per
  refresh instead of once per game, and game/art matching now uses an exact id
  boundary, fixing a rare case where one game's id being a prefix of another's
  could mis-report "has artwork".
- The API key and the Steam-running status are now cached briefly in memory,
  removing repeated disk reads and process scans during fetches and UI updates.

### Fixed
- **The main window now stays fully on-screen.** Its size/position is clamped to
  the screen work area (like the results window), so a position saved on a larger
  or different display — e.g. the Deck docked then undocked — can no longer strand
  it off-screen or under the taskbar.
- Settings window placement now uses the same work-area-aware logic.
- Removed an open-then-resize flash when the main and Settings windows appear.

### Development
- Extracted the pure window-placement math into `find_games.py` with regression
  tests; expanded the test suite (now 81 tests).

## [1.2.0] — 2026-06-26

### Added
- **Full color-icon UI.** Every interface glyph that used to be a monochrome/box
  emoji (toolbar, per-game status, results controls, activity log, status bar,
  Steam status, API-key eye, etc.) is now a bundled [Fluent Emoji](https://github.com/microsoft/fluentui-emoji)
  PNG loaded via Pillow, so the app's visual flair renders consistently on every
  platform instead of depending on the system Tk's emoji support.
- Single-click in the API-key field now selects the whole key, so it can be
  replaced or copied without double-clicking and dragging.

### Changed
- **Redesigned results screen.** Each art type is now a clearly titled card with a
  responsive layout: the thumbnail sits centered between the ◀/▶ cycle buttons and
  the Apply button stretches the full card width beneath them. Layout adapts to
  different window sizes and screen resolutions.
- Icon sizes were unified into a coherent, readable scale across the whole UI.

### Fixed
- **Self-restart no longer crashes on Windows** (e.g. toggling light/dark theme).
  The relaunch now strips the PyInstaller `_MEI`/`_PYI` environment so the new
  process doesn't try to reuse the old one's deleted temp directory.
- **Results window snaps fully into view.** It opens at the classic 900×750 (or
  smaller on small screens) positioned entirely within the screen work area, so
  its title bar and the bottom action buttons are never hidden behind the taskbar
  or a desktop panel.
- **No more open-then-resize flash.** The main, Settings and results windows are
  built hidden and revealed once already sized and positioned.

### Development
- Added `make_winbuild.sh`, which produces a self-contained Windows test-build
  bundle (source + pre-fetched wheels + offline/online build instructions).

## [1.1.0] — 2026-06-25

### Added
- **Wide Cover art** — landscape grid images (460×215 / 920×430) are now fetched
  and applied alongside the portrait cover. These show up in Big Picture mode and
  the legacy grid view. The Wide Cover row appears in the results screen with the
  same cycle/swap alternatives as every other art type.

### Changed
- **Animated covers now actually animate in Steam.** Steam's grid folder only renders
  PNG/JPG and animates only APNG — it silently ignores the `.webp` that SteamGridDB
  serves, which is why animated picks never showed up before. Animated artwork is now
  converted to full-quality APNG on apply. Because that conversion is heavy, animated
  art is **not** auto-applied during a fetch: a static cover is applied as the default
  and animated options appear as alternatives in the results screen. Click **Apply this
  one** on an animated option to convert and commit it; a progress popup shows the
  frame-by-frame conversion.

### Fixed
- **Artwork whose format changes now applies correctly.** Steam keys artwork off the
  filename base (e.g. `12345p`), so writing a new file with a different extension used
  to leave the old one behind and Steam kept showing it. Both the initial fetch and the
  results-screen swap now clear the previous file in a slot first. Previous artwork is
  still backed up, so Undo continues to work.
- Portrait cover fetches are now constrained to portrait dimensions, so a wide grid
  can no longer accidentally land in the cover slot.
- Transient network/parse errors during search no longer cause a game to be skipped
  permanently — only a genuine "no results" response skips it.
- The animated-conversion popup now shows accurate real per-frame progress (read from
  the APNG frames as they encode) instead of a bar that filled instantly then stalled,
  and it stays correctly stacked within the app's own window chain.

### Robustness
- SteamGridDB requests now retry on HTTP 429 (rate-limit), honoring `Retry-After`,
  so large libraries no longer silently miss artwork when throttled.
- The fetch worker thread no longer touches Tk widgets directly; all UI updates are
  marshaled to the main thread, removing a class of intermittent crashes/hangs.
- Downloads are now written to a temp file and atomically swapped into place only
  after a completeness check (Content-Length), so a dropped connection can't leave a
  truncated image that gets applied or tracked as managed art.
- Icons are written to every shortcut sharing an appid, not just the first.
- Set `STEAMART_DEBUG=1` to print the detail behind otherwise-silent failures.
- Internal cleanups: timezone-aware date parsing, skip-list de-duplication, unified
  URL quoting, and removal of dead code.

### Development
- Added a `pytest` suite (`tests/`, `requirements-dev.txt`) covering the pure logic in
  `find_games.py` — pref/param mapping, slot clearing, file round-trips, APNG
  conversion/progress, and 429 retry — plus a static-analysis cleanup pass.
- Added a GitHub Actions release workflow that builds the Linux and Windows binaries in
  a clean environment and attaches them to the Release whenever a `v*` tag is pushed.

## [1.0.0] — 2026-06-08

### Added
- Initial public release.
- Detects non-Steam shortcuts and fetches Cover, Hero, Logo, and Icon artwork from
  SteamGridDB.
- Results screen with up to 5 swappable alternatives per art type.
- Icon support written directly into `shortcuts.vdf`.
- Art Style Preferences (animated, NSFW, humor, per-type style variants).
- Live SGDB search with autocomplete, skip list, undo, re-fetch, multi-account
  detection, thumbnail cache, dark/light themes, and factory reset.
- Cross-platform: Linux (Steam Deck) and Windows.

[1.2.5]: https://github.com/Rhastago/NonSteamScraper/releases/tag/v1.2.5
[1.2.0]: https://github.com/Rhastago/NonSteamScraper/releases/tag/v1.2.0
[1.1.0]: https://github.com/Rhastago/NonSteamScraper/releases/tag/v1.1.0
[1.0.0]: https://github.com/Rhastago/NonSteamScraper/releases/tag/v1.0.0
