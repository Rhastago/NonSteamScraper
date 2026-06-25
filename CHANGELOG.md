# Changelog

All notable changes to NonSteamScraper are documented here.

This project adheres to [Semantic Versioning](https://semver.org).

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

[1.1.0]: https://github.com/Rhastago/NonSteamScraper/releases/tag/v1.1.0
[1.0.0]: https://github.com/Rhastago/NonSteamScraper/releases/tag/v1.0.0
