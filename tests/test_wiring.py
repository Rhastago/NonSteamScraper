"""AST-shape wiring checks for S3 (restore point) and S1 (relaunch env).

These tests parse the module sources with `ast` and assert STRUCTURE, never
token presence — a substring check is satisfied by a comment or by inverted
logic. The modules are NOT imported: they pull in tkinter and would need a
display, and the backend binds HOME-derived paths at import (it once created directories
under the real HOME).

The product wiring does not exist yet, so each gate fails by ASSERTION with a
message — never by an AttributeError from a missing symbol.
"""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _module(name):
    path = os.path.join(ROOT, name)
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=name)


def _func(module, name):
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_named(node, name):
    """All Call nodes inside `node` whose callee's final name is `name`.
    Handles both the Name form (`snapshot_for_restore(...)`) and the Attribute
    form (`restore.snapshot_for_restore(...)`), matching on the final attribute name."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            callee = sub.func
            final = None
            if isinstance(callee, ast.Name):
                final = callee.id
            elif isinstance(callee, ast.Attribute):
                final = callee.attr
            if final == name:
                out.append(sub)
    return out


def _unlink_calls(node):
    """Every call that removes a file, whatever it is spelled: os.remove,
    os.unlink, Path.unlink. Gating on `os.remove` alone is wrong in BOTH
    directions — it fails a correct implementation that uses os.unlink, and it
    lets a leftover os.unlink double-delete slip through."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr in ("remove", "unlink"):
                out.append(sub)
    return out


def _contains(outer, inner):
    """True if `inner` is a descendant of `outer` — containment, not line order."""
    return any(n is inner for n in ast.walk(outer))


def _references_name(node, name):
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def _glob_calls(node):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "glob" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "glob"]


def test_wiring_library_and_search_delete_nothing_themselves():
    """B1: the Search dialog once deleted with glob(f"{app_id}*") and took a
    neighbouring game's art. Re-fetch and Search → Select now clear through
    operations.refetch / operations.rematch (tested by behaviour in
    test_operations.py and test_fetch_flow.py); these two UI files must not glob
    the grid folder or delete a file anywhere."""
    for name in ("library_mixin.py", "dialogs.py"):
        tree = _module(name)
        assert not _glob_calls(tree), f"{name} must not glob the grid folder"
        assert not _unlink_calls(tree), f"{name} must not delete files itself"


def test_wiring_undo_fetch_delegates_to_undo_restore_point():
    """undo_fetch must delegate to undo_restore_point."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "undo_fetch")
    assert fn is not None, "fetch_mixin.undo_fetch does not exist"
    calls = _calls_named(fn, "undo_restore_point")
    assert calls, "fetch_mixin.undo_fetch must call undo_restore_point"


def test_wiring_undo_fetch_has_no_os_remove_copy():
    """Deletion moved into restore.undo_restore_point — a leftover os.remove
    here would double-delete."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "undo_fetch")
    assert fn is not None, "fetch_mixin.undo_fetch does not exist"
    hits = _unlink_calls(fn)
    assert not hits, (
        "fetch_mixin.undo_fetch must not delete files (os.remove/os.unlink) — "
        "deletion moved into restore.undo_restore_point")


def test_wiring_last_fetch_files_attribute_gone_from_fetch_mixin():
    """self.last_fetch_files is replaced by has_restore_point(); any surviving
    Attribute reference means the old bookkeeping is still driving Undo."""
    tree = _module("fetch_mixin.py")
    attrs = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "last_fetch_files"]
    assert not attrs, (
        "fetch_mixin.py must no longer reference last_fetch_files (replaced by "
        "has_restore_point()); found at line(s): %s"
        % sorted(a.lineno for a in attrs))


def test_wiring_settings_window_clears_restore_point():
    """The Settings Storage row must wire clear_restore_point."""
    tree = _module("settings_window.py")
    calls = _calls_named(tree, "clear_restore_point")
    assert calls, "settings_window.py must call clear_restore_point"
    # Both halves of the operator's chosen retention answer: the restore point
    # AND the legacy ~/.steamart_backup pile, which has never had a caller.
    legacy = _calls_named(tree, "clear_backup")
    assert legacy, "settings_window.py must also call clear_backup (the legacy pile)"
    sizes = _calls_named(tree, "restore_point_size") + _calls_named(tree, "legacy_backup_size")
    assert len(sizes) >= 2, (
        "the Storage row must show both restore_point_size() and legacy_backup_size()")


def test_wiring_undo_button_derived_from_restore_point_in_load_games():
    """Pinned, not merely present: library_mixin defines _refresh_undo_button,
    that helper calls has_restore_point, and load_games calls the helper. A call
    to has_restore_point somewhere in the file would pass while the button-state
    requirement shipped unchecked — the criterion is about where it runs."""
    tree = _module("library_mixin.py")
    refresh = _func(tree, "_refresh_undo_button")
    assert refresh is not None, "library_mixin._refresh_undo_button does not exist"
    has = _calls_named(refresh, "has_restore_point")
    assert has, "_refresh_undo_button must call has_restore_point() to derive the button state"
    load = _func(tree, "load_games")
    assert load is not None, "library_mixin.load_games does not exist"
    refs = _calls_named(load, "_refresh_undo_button")
    assert refs, "load_games must call _refresh_undo_button (the single place the button state is derived)"


def test_wiring_relaunch_app_uses_relaunch_env():
    """The headline S1 fix's only behavioural gate: relaunch_app must re-exec
    with the normalized environment. Without this, a relaunch that re-execs with
    dict(os.environ) passes every other gate while bug #1 ships unfixed."""
    tree = _module("ui_mixin.py")
    fn = _func(tree, "relaunch_app")
    assert fn is not None, "ui_mixin.relaunch_app does not exist"
    calls = _calls_named(fn, "relaunch_env")
    assert calls, "ui_mixin.relaunch_app must call relaunch_env instead of re-execing with dict(os.environ)"


def test_wiring_refetch_game_has_no_glob_prefix_delete():
    """refetch_game's delete loop must move to leading-digit-run matching — a
    surviving glob(f'{app_id}*') is the cross-game deletion bug still present."""
    tree = _module("library_mixin.py")
    fn = _func(tree, "refetch_game")
    assert fn is not None, "library_mixin.refetch_game does not exist"
    hits = [n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "glob" and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "glob"]
    assert not hits, (
        "refetch_game must not glob(f'{app_id}*') — that deletes a neighbouring "
        "game's art; use leading-digit-run matching instead")


def test_wiring_undo_fetch_confirms_an_inherited_restore_point():
    """Undo is destructive, cannot itself be undone, and its state now comes
    from disk — so it can sit enabled for weeks after a fetch the operator
    accepted. The guard: confirm when the restore point was inherited from a
    previous session (and no fetch has happened since). Without this gate an
    executor can ship the thin delegate with no confirmation and pass
    everything else."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "undo_fetch")
    assert fn is not None, "fetch_mixin.undo_fetch does not exist"
    asks = _calls_named(fn, "askyesno")
    assert asks, (
        "undo_fetch must confirm before destroying an inherited restore point "
        "(messagebox.askyesno)")
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "_restore_point_inherited" in attrs, (
        "the confirmation must be scoped by self._restore_point_inherited, so the "
        "ordinary fetch -> dislike -> Undo flow stays a single click")
    assert "_fetched_this_session" in attrs, (
        "the confirmation must also check self._fetched_this_session")


def test_wiring_app_captures_restore_point_inherited_at_startup():
    """_restore_point_inherited is captured ONCE at startup — later it would
    always be True and the confirmation would fire on every undo."""
    tree = _module("app.py")
    assigned = [n for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == "_restore_point_inherited"]
    assert assigned, "app.py must capture self._restore_point_inherited at startup"
    has = _calls_named(tree, "has_restore_point")
    assert has, "app.py must initialise it from restore.has_restore_point()"


def test_wiring_fetch_marks_the_session_as_having_fetched():
    """_run_fetch_body must set _fetched_this_session, or an inherited restore
    point that the operator has since replaced with a fresh fetch would still
    prompt."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "_run_fetch_body")
    assert fn is not None, "fetch_mixin._run_fetch_body does not exist"
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "_fetched_this_session" in attrs, (
        "_run_fetch_body must set self._fetched_this_session")


def _first_stmt_refuses_if_busy(fn):
    """True when fn's first statement is `if <x>._refuse_if_busy(): return`."""
    if not fn.body or not isinstance(fn.body[0], ast.If):
        return False
    first = fn.body[0]
    return (bool(_calls_named(first.test, "_refuse_if_busy"))
            and any(isinstance(n, ast.Return) for n in first.body))


def test_wiring_settings_write_commands_refuse_while_a_fetch_runs():
    """Q3: in Settings, Clear saved undo data, Clear All Artwork, Factory reset
    and the account switch must refuse first thing while a fetch runs. (Settings
    cannot be built headless, so this is a structure check; the live behaviour
    is on the manual-test list.)"""
    tree = _module("settings_window.py")
    for name in ("do_clear_restore", "do_clear_artwork", "do_full_reset", "on_account_change"):
        fn = _func(tree, name)
        assert fn is not None, f"settings_window.{name} does not exist"
        assert _first_stmt_refuses_if_busy(fn), (
            f"settings_window.{name} must start with `if app._refuse_if_busy(): return`")


def test_wiring_results_window_does_no_file_work_itself():
    """B6: results "Apply this one" used to download on the Tk thread and write the
    grid file with no .part. The worker/thread/outcome order is now tested by
    behaviour (test_fetch_flow: test_results_static_apply_installs_on_a_worker_...,
    test_results_animated_icon_apply_...), which replaced the AST checks of the
    worker's call names. What stays here: the file does no file work of its own."""
    tree = _module("results_window.py")
    assert not _calls_named(tree, "http_get"), "results_window.py must not download itself"
    assert not _calls_named(tree, "clear_slot_files"), (
        "results_window.py must not clear slots itself — install_into_slot does")
    opens = [c for c in _calls_named(tree, "open") if isinstance(c.func, ast.Name)]
    assert not opens, "results_window.py must not write files itself"


def _pyinstaller_flags(script):
    """The flags of the pyinstaller command in a build script, one per item."""
    with open(os.path.join(ROOT, script), encoding="utf-8") as f:
        text = f.read()
    cmd = text[text.index("\npyinstaller ") + 1:]
    cmd = cmd[:cmd.index("app.py")].replace("\\\n", " ")
    return cmd.split()


def test_build_scripts_use_the_same_pyinstaller_flags():
    """H6: the Linux and Windows scripts drifted apart (different hidden imports).
    They must differ only in the --add-data separator and Windows' --icon."""
    linux = _pyinstaller_flags("build_linux.sh")
    windows = [f.replace(";", ":") for f in _pyinstaller_flags("build_windows.sh")]
    i = windows.index("--icon")
    del windows[i:i + 2]
    assert linux == windows


def test_find_games_is_gone_and_nothing_imports_it():
    """H4/T-A5: the god module is split; its leftover CLI (it wrote icons while
    Steam could be running and bypassed the restore point) is deleted."""
    assert not os.path.exists(os.path.join(ROOT, "find_games.py"))
    for name in os.listdir(ROOT):
        if name.endswith(".py"):
            tree = _module(name)
            mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            assert "find_games" not in mods, f"{name} still imports find_games"


def test_app_takes_the_one_copy_lock_before_anything_else():
    """W8 + R2: app.py's __main__ creates the data folder (the lock file lives in
    it, and an unopenable lock file lets the app start), then asks for the lock and
    exits when another copy holds it, then moves an older version's files, and only
    then lets steam_env.init() read the saved account — all before the window."""
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        src = f.read()
    main = src[src.index('if __name__ == "__main__":'):]
    folder = main.index("appfiles.ensure_data_folder()")
    lock = main.index("appfiles.acquire_instance_lock()")
    migrate = main.index("appfiles.migrate_legacy_files()")
    assert folder < lock < migrate < main.index("steam_env.init()") < main.index("window = tk.Tk()")
    assert "sys.exit(0)" in main[lock:migrate]
    # an older copy still holding its lock: this copy does not start either
    assert 'migrate_legacy_files()["skipped"]' in main
    assert "sys.exit(0)" in main[migrate:main.index("steam_env.init()")]


def test_the_build_scripts_carry_no_collect_all_flags():
    """N23: --collect-all pillow collected nothing (the package is PIL), and
    --collect-all multiprocessing bundled modules the app never imports."""
    for script in ("build_linux.sh", "build_windows.sh"):
        assert "--collect-all" not in _pyinstaller_flags(script), script


def _pins():
    with open(os.path.join(ROOT, "constraints.txt"), encoding="utf-8") as f:
        lines = [l.split("#")[0].strip() for l in f]
    return {l.split(";")[0].split("==")[0].lower(): l for l in lines if l}


def test_every_dependency_is_pinned_to_one_version():
    """N21: every dependency was a >= floor, so CI shipped whatever was newest on
    tag day. constraints.txt pins each requirement to an exact version."""
    pins = _pins()
    assert all("==" in line for line in pins.values()), pins
    for req in ("requirements.txt", "requirements-build.txt", "requirements-dev.txt"):
        with open(os.path.join(ROOT, req), encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if not line or line.startswith("-r"):
                    continue
                name = line.split(">")[0].split("=")[0].split("<")[0].strip().lower()
                assert name in pins, f"{req}: {name} is not pinned in constraints.txt"


def test_every_install_and_download_uses_the_pins_and_python_3_13():
    """N21 + ruling Q8: CI, the build scripts and the Windows bundle install the
    pinned versions, all on Python 3.13."""
    with open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8") as f:
        ci = f.read()
    installs = [l for l in ci.splitlines() if "pip install -r" in l]
    assert installs and all("-c constraints.txt" in l for l in installs), installs
    assert "python-version: '3.13'" in ci and "3.12" not in ci
    for script in ("build_linux.sh", "build_windows.sh"):
        with open(os.path.join(ROOT, script), encoding="utf-8") as f:
            assert "-r requirements-build.txt -c constraints.txt" in f.read(), script
    with open(os.path.join(ROOT, "make_winbuild.sh"), encoding="utf-8") as f:
        wb = f.read()
    assert wb.count("--python-version 3.13") == 2 and "3.12" not in wb
    assert "-c constraints.txt \\" in wb and "constraints.txt tests" in wb
