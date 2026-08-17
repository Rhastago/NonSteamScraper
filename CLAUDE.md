# CLAUDE.md

NonSteamScraper — a tkinter desktop app (Linux/Steam Deck + Windows) that finds non-Steam
shortcuts, fetches artwork from SteamGridDB, and writes it into the user's **real Steam
installation**. Shipped as a PyInstaller binary, not as source. Read `README.md` for what it does
and `CHANGELOG.md` for what changed.

`REASONIX.md` holds the same non-negotiables in the form a code-executing agent reads. When you
change an invariant, change it in both.

## How to work here

- **BUILD WHAT WAS ASKED FOR. NOTHING ELSE.** If a requirement seems thin, ask; do not fill the gap
  with a feature. If something genuinely cannot be done, say so and stop — do not substitute a
  nearby thing that was never requested and present it as done. Scope you added yourself is scope
  you will be asked to delete.
- **Root-cause first.** Say what is actually broken before writing code. A fix without a named
  cause gets rejected.
- **Research, never guess.** Unsure about a tkinter or SteamGridDB API? Look it up and cite it.
  Guessed syntax is worse than an admitted gap.
- **Ambiguous? Stop and ask** with a short numbered questionnaire. Keep executing every task that
  does *not* depend on the answer while you wait, and say which tasks are blocked on which question.
- **Route to DeepSeek** (`plan-execute-verify-deepseek` skill; **`DS` is the codeword** — "DS it",
  "send it to DS", "DS: <task>" all mean route it there. The codeword asks for the routing; it does
  not override the test that follows) when all three hold: the change is bounded to named files,
  acceptance is checkable by a shell command that is *capable of failing*, and no business-logic
  judgment is needed. That covers implementing to spec, codemods and renames, tests in
  `tests/test_find_games.py`, boilerplate, and read-only codebase scouting. Prefer `deepseek-flash`;
  escalate to `deepseek-pro` only after two failures on a sharpened task file. **Keep yourself:**
  anything that writes into Steam's own files (`shortcuts.vdf`, the grid folder), the deletion
  semantics of `clear_slot_files` / `refetch_game` / `full_reset`, the tkinter threading and
  `after(0, ...)` marshalling, and the `results`-dict consumption contract — those are exactly where
  a subtle error costs the user their shortcuts or silently hangs the app. **If you cannot write a
  failing-capable verify command, the task is underspecified — fix the spec, do not dispatch it.**
  Re-run every gate yourself; never accept an executor's claim that it passed, and treat a green
  gate over a wrong diff as a failure. DeepSeek never commits, and it cannot see the GUI.
- **Over-keeping is how that rule gets broken.** The three-part test is not a permission you may
  decline: **a task that passes it is dispatched.** Three standing corrections:
  - **Keep the decision, dispatch the typing.** "Keep yourself" names the *judgment*, not the
    mechanical work that follows once the judgment is made. Once the behaviour is decided,
    implementing it is bounded, named-file, gate-checkable work — dispatch it.
  - **Proximity to a trap is not a keep reason.** A rename, a widget-label change, or a new test is
    dispatchable even when it lives in `find_games.py`. Name the trap that applies to *this diff*,
    or dispatch it.
  - **Write the verdict per task at plan time, one line:** `T4 → DS (files, gate)` or
    `T4 → keep (<named trap>)`. A task with no written verdict was drifted into, not routed. About
    to hand-write something with a named file scope and a failing-capable gate? That is the tell.
  A weak gate is the real blocker, not the difficulty of the code: a string-match assertion where
  the requirement is behavioural is not a gate you can dispatch behind. Sharpen the gate — that is
  the work, not writing the diff yourself.
- **Verify on the real thing.** "146 tests pass" is not "it works" — every test runs against a
  sandboxed `HOME` with the network monkeypatched, so no test proves the app launches, the window
  lays out, or a fetch succeeds. A GUI or network change is **UNVERIFIED** until the app has been
  run and looked at. Say plainly which paths have never been exercised live.
- **Never imply coverage you do not have.** Listing three things as unverified while silently
  omitting a fourth reads as "the fourth is fine".
- **Never point the app at the developer's real Steam data to test.** Sandbox `HOME` the way the
  suite does. Importing `find_games` alone creates directories under the real `HOME`.
- **CLEAN UP WHAT THE TESTING LEFT, EVERY TIME.** Temp dirs, scratch art, `.plan/` runs, stray
  binaries in `dist/`. Before saying a task is done, look for what the work left behind and say what
  you removed. If a leftover has to survive, name it and say why.
- Design/UX calls may be handed back to you ("your call") — decide, state why, keep going.
- Report failures plainly. Say what is untested; do not imply coverage that does not exist.

## Non-negotiables

- **Writes into Steam's files stay atomic.** `_write_shortcuts_atomic` (temp + `os.replace`) and the
  `.part` + `os.replace` download path exist so a crash cannot truncate `shortcuts.vdf` or leave a
  half-written image.
- **Icon writes are lost while Steam is running.** The pending-icon queue and its poll are the
  design, not an accident. Never bypass them.
- **`appid` is masked `& 0xFFFFFFFF`** everywhere. Unmasking breaks shortcut matching and every grid
  filename.
- **`import find_games as fg`, read globals live.** Account switching rebinds `fg.GRID_FOLDER` and
  `fg.SHORTCUTS_PATH`; a by-value import keeps writing to the old account.
- **Never touch a widget or build a `PhotoImage` off the Tk thread**; marshal with
  `self._ui(fn)`. Never raise a messagebox from a worker thread.
- **The `results` dict mixes slot dicts with a tuple.** Consume it only via
  `applied_paths_from_results()` / `icon_write_from_results()`; a crash there hangs the UI silently.
- **No type hints, no `logging` module** in production code — `print()` plus `_debug()` behind
  `STEAMART_DEBUG`. There is no linter and no lint config; do not add one or reformat wholesale.
- Deletion paths are deliberate, including the surprising ones. Never add a new deletion of user
  files.

## Environment

- Python **3.13** in `venv/` (CI builds on 3.12). Runtime deps in `requirements.txt`; `pytest` is
  the only dev dep.
- Tests: `venv/bin/python -m pytest -q` — 146 tests, well under a second.
- No linter is installed. There is no lint gate; do not invent one.
- Builds: `./build_linux.sh` / `./build_windows.sh` (PyInstaller). CI builds binaries only on a
  `v*` tag and runs no tests.
- `.claude/` and `.plan/` are gitignored — plan artefacts and local settings never get committed.
