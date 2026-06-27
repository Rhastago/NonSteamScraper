# Changelog

All notable changes to NonSteamScraper are documented here.

This project adheres to [Semantic Versioning](https://semver.org).

## [1.4.2] — 2026-06-28

A round of bug fixes from a code-review pass, an icon-status consistency fix, and a Windows popup-flash fix.

### Fixed
- **Results screen — icons no longer claim to be applied before they are.** When an icon is applied (or auto-applied during a fetch) while Steam is running, it can't be written to `shortcuts.vdf` yet, so it's queued. The results screen now shows that icon as **"Queued (close Steam)"** with no accent border until it's actually written; the border and **"Applied!"** now always mean the icon really landed. Previously a queued icon could misleadingly read "Applied!" with a border.
- **Results screen — failed swaps now say so.** Applying an alternative whose download fails (a non-200 response) now shows **"Failed — retry"** instead of silently doing nothing.
- **Windows — popups no longer flash.** The Quick Start guide, Art Style Preferences, and SteamGridDB Search windows no longer momentarily appear at the wrong size/position before settling; they now open directly at their final geometry.
- **Quick Start guide copy.** Corrected stale text — the per-game button is **"Search"** (not "Rename"), a note that icons apply automatically once Steam is closed, and clearer wording that **"Re-fetch"** clears a game's art for re-download on the next fetch.

### Changed
- **Faster artwork cycling.** Paging back and forth through alternatives (◀/▶) on the results screen is now instant on revisit — each option's preview is cached after first decode — while animated previews still animate.

### Internal
- Hardened animated-artwork apply against the results window being closed mid-conversion.
- Cheaper Steam-status polling (skips parsing the pending-icons file when nothing is queued).
- Code-review cleanup: removed a dead geometry method and unused result-slot fields, unified the modal-reveal helper, and centralized the results apply-button/border logic into a single source of truth.

## [1.4.1] — 2026-06-27

A small results-screen refinement, plus a large internal refactor and new tests (no behavior change).

### Changed
- **Results screen:** the artwork option currently applied to Steam is now framed with an accent-colored border as you cycle through the alternatives, so it's obvious at a glance which one is live versus which are alternatives you're previewing.

### Internal
- Split the ~3,000-line `app.py` into focused modules — themed-widget / geometry / library / fetch mixins, plus dedicated Settings, Results, and dialog modules — for maintainability. Behavior-preserving.
- Added fetch-pipeline integration tests covering the artwork download/apply seam (146 tests total).

## [1.4.0] — 2026-06-27

A broad visual redesign plus a few correctness fixes.

### Added
- **Accent color themes.** Settings → Appearance now offers four accent colors
  (Steam Blue, Vibrant Green, Purple, Teal) on top of light/dark mode. The accent
  threads through the whole UI — the primary buttons, the selected game row, the
  progress bar, a title underline, section headings, focus rings, and links — so
  the app feels themed rather than flat grey. Your choice persists between sessions.
- **Cover thumbnails in the game list.** Each game row now shows a thumbnail of its
  current cover art (or a clean "needs art" placeholder), decoded off the UI thread
  and cached so scrolling and live-search stay smooth. Each game's row is wrapped
  in a slim accent border.
- **Sort the game list.** A sort control next to the search box orders games by
  Name, Date added, Missing artwork, or Recently fetched artwork — ascending or
  descending — and works together with the live search filter.
- **First-run onboarding.** When no API key is set, a banner guides you straight to
  Settings to add one; it disappears the moment a valid key is saved.

### Changed
- **Prominent primary action.** The "Fetch Missing Artwork" button — and the key
  call-to-action in every other window — is now an accent-colored button instead of
  another grey one.
- **Cleaner typography.** Game names, search, and dialog text moved off the
  monospace font to a proper UI font; the activity log and API-key field stay
  monospace where it helps.
- **Activity log tucked into a "Details" drawer.** It's collapsed by default (with a
  dot when there's new activity) so the main window stays uncluttered; the status bar
  and progress bar carry the everyday feedback. The window auto-sizes when the drawer
  opens or closes so nothing is clipped.
- **Clearer Art Style Preferences.** The three states were relabeled from
  Always/OK/Never to **Prefer / Allow / Exclude** across every filter (content and
  style alike) — matching how the fetch actually works (Prefer is a soft preference
  that broadens automatically; only Exclude is a hard filter). The neutral "Allow"
  default is no longer colored like a warning, and every row has a hover tooltip
  explaining it.
- **The main window now opens centered** on the screen each launch instead of
  reopening at its last position.
- **Bigger, touch-friendlier row buttons** (Re-fetch / Search / Reset Skip) for the
  Steam Deck.
- The "Reloading library…" status now reports when it's done.

### Fixed
- **Factory Reset no longer deletes your artwork.** "Reset App to Factory Defaults"
  now clears only settings and the regenerable cache — your fetched/applied art and
  the managed-file registry are preserved. Use "Clear All Artwork" if you also want
  to remove art. (The confirmation dialog and docs were corrected to match.)
- **Per-account pending icons.** Icons queued while Steam is open are now namespaced
  per Steam account, so switching accounts can no longer apply or discard another
  account's queued icons (a data-loss bug on multi-account setups). A legacy queue is
  migrated to the active account on upgrade.
- The two "Restart Steam" buttons no longer freeze the UI for a few seconds (the
  restart runs off the UI thread).

### Development
- Expanded the test suite to 141 tests (added coverage for the per-account icon
  queue, factory-reset art preservation, and cover-thumbnail path selection).

## [1.3.0] — 2026-06-27

### Added
- **Multiple Steam accounts.** The app now detects every account that has signed
  in on this machine and lets you switch between them from Settings. The account
  picker is always visible — greyed out when only one account exists — and the
  app remembers your selection between sessions, fetching artwork for whichever
  account is active.
- **In-app update check.** Settings shows your current version and a "Check for
  updates" button that compares against the latest GitHub release. It clearly
  reports "up to date", an available update with a direct link to the releases
  page, or a friendly message if GitHub's rate limit is hit (with a "try again
  in ~N min" hint). A "View releases page" link is always available.
- **Deferred / auto-apply icons.** Steam overwrites `shortcuts.vdf` on exit, so
  icons can only be written safely while Steam is closed. If Steam is running
  when you fetch, icons are now queued instead of lost, and the app applies them
  automatically the moment Steam closes — even across app restarts. The results
  screen offers a one-click "Close Steam & Apply Icons" action and shows how many
  icons are pending.

### Changed
- **Smoother UI under load.** Image decoding now happens off the UI thread, so
  browsing results and cycling alternatives stays responsive while artwork loads.
- **Window modality chaining.** Settings, the results window, and all dialogs
  (information, art-style preferences, etc.) now layer correctly — the most
  recently opened window stays in front and holds focus, and closing it returns
  focus to the one beneath. No more dialogs hiding behind the main window.
- Icon writes to `shortcuts.vdf` are now batched and written atomically (temp
  file + replace), so a single fetch updates all icons in one safe pass.

### Fixed
- Fetching no longer hangs when Steam is open — icons are deferred cleanly
  instead of stalling the fetch.
- The results-screen loader now animates reliably beneath the action buttons.
- The "icons pending" count now updates correctly as more icons are queued.
- Removed status-line flicker on the results screen's Steam-state polling.

### Development
- Expanded the test suite to 131 tests, including regression coverage for the
  deferred-icon path and the update-check / rate-limit handling.

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
