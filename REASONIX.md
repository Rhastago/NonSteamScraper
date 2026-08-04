# Project rules — binding on every task

NonSteamScraper: a tkinter desktop app that fetches artwork from SteamGridDB and
writes it into a user's real Steam installation. Read this before editing
anything. These are not style preferences — violating one fails the task even if
the verify command exits 0.

## Tests and lint

- The suite is `venv/bin/python -m pytest -q` from the repo root. 146 tests,
  under a second. Keep it green; a red suite is a real finding.
- `tests/test_find_games.py` sets `HOME`/`USERPROFILE` to a throwaway temp dir
  **before** importing `find_games` (tests/test_find_games.py:30-35). Never
  reorder those lines, never import `find_games` above them, and never remove
  the sandbox — without it the tests read and write the developer's real Steam
  data.
- Never weaken an assertion, skip a test, or special-case test input to make a
  gate pass. That is a task failure even when the command exits 0.
- There is **no ruff config, no linter installed, and no lint gate**. Do not add
  one, do not reformat files wholesale, and do not "fix" style you were not
  asked to touch.

## Importing the backend

- `import find_games` has **side effects at module scope**: it detects the Steam
  install and calls `os.makedirs(GRID_FOLDER, exist_ok=True)`
  (find_games.py:192-216). Any throwaway script that imports it with the real
  `HOME` touches the user's actual Steam directories. Sandbox `HOME` first, or
  do not import it.
- Always `import find_games as fg` and read `fg.GRID_FOLDER` / `fg.SHORTCUTS_PATH`
  **live**. Never `from find_games import GRID_FOLDER` — switching Steam account
  rebinds those module globals, and a by-value import silently keeps writing to
  the previous account (app.py:5, library_mixin.py:457-458).

## Threading and the UI

- tkinter, one `mainloop()` on one thread. **Never touch a widget, and never
  create a `PhotoImage`, off the Tk thread.** Marshal back with `self._ui(fn)`
  (`window.after(0, fn)`, geometry_mixin.py:253-255).
- **Never open a messagebox or modal dialog from a worker thread.** It hangs the
  app; this is documented in the code (find_games.py:281-289).
- Hold a reference to every `PhotoImage` you create (`widget.image = photo`).
  A dropped reference is garbage-collected and the image renders blank.
- Honour the staleness tokens (`_load_token` / `_thumb_token`, imaging.py:67-93)
  when adding any asynchronous decode, or a slow decode overwrites a newer image.
- Self-rescheduling `after` polls must stay cheap and must stop when the window
  is gone (fetch_mixin.py:77-81, results_window.py:234-247).

## The fetch results structure

- The `results` dict mixes per-slot dicts with an `icon_to_set` **tuple**.
  Consume it only through `fg.applied_paths_from_results()` and
  `fg.icon_write_from_results()` (find_games.py:1068-1096). A naive
  `for v in results.values(): v.get(...)` raises inside a daemon thread, which
  does not surface — the UI simply hangs on "Fetching…" for ever.

## Writing to the user's machine

- Writes to Steam's `shortcuts.vdf` go through `_write_shortcuts_atomic`
  (temp file + `os.replace`, find_games.py:538-546); artwork downloads go
  through `.part` + `os.replace` (find_games.py:784-808). **Keep both atomic.**
  A truncated `shortcuts.vdf` costs the user their whole shortcut list.
- **Icon writes are lost while Steam is running** — Steam rewrites the file on
  exit. That is why the pending-icon queue and its poll exist
  (find_games.py:281-290, 644-663). Never "simplify" `set_shortcut_icons` to
  write directly and skip the queue.
- `appid` is masked to unsigned 32-bit (`appid & 0xFFFFFFFF`) everywhere
  (find_games.py:531, 564, 946-951). Unmasking it breaks shortcut matching and
  every grid filename.
- Deletion semantics are deliberate, including the surprising ones
  (`clear_slot_files` removes art this app never registered,
  find_games.py:810-830). Do not widen, narrow, or "tidy" any deletion path
  unless the task says to. Never add a new deletion of user files.
- Never run the app's destructive helpers (`full_reset`, `clear_managed_artwork`,
  `stop_steam`, `restart_steam`) against the real machine while developing.

## Style

- Production code uses **no type annotations** and **no `logging` module** —
  `print()` plus `_debug()` behind `STEAMART_DEBUG` (find_games.py:11-21).
  Match that; do not introduce either.
- `snake_case` functions, `UPPER_SNAKE` path constants, `_`-prefixed privates,
  mixin classes named `XxxMixin`, tests named `test_<fn>_<scenario>`.
- Failures in background paths are swallowed and reported through `_debug` or
  the in-app log; several functions contractually never raise
  (find_games.py:622, 653, 1219). Preserve that contract.
- UTF-8 everywhere. User-facing strings are sentence-case and may contain the
  existing glyphs (◀ ▶ ▲ ▼ 🔍 🎨 ⚙ ℹ) — do not add new emoji to source.

## Scope

- macOS paths exist in `find_games.py` but macOS is declared unsupported
  (README.md). Do not build on them unless asked.
- Do not edit `CHANGELOG.md`, `README.md`, or bump `VERSION` unless the task
  says to.
