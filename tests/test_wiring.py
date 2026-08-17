"""AST-shape wiring checks for S3 (restore point) and S1 (relaunch env).

These tests parse the module sources with `ast` and assert STRUCTURE, never
token presence — a substring check is satisfied by a comment or by inverted
logic. The modules are NOT imported: they pull in tkinter and would need a
display, and find_games has import-time side effects (it creates directories
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
    form (`fg.snapshot_for_restore(...)`), matching on the final attribute name."""
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


def test_wiring_refetch_game_snapshots_before_it_deletes():
    """snapshot_for_restore must run BEFORE os.remove in refetch_game —
    reversed, it snapshots an already-empty folder and Undo restores nothing."""
    tree = _module("library_mixin.py")
    fn = _func(tree, "refetch_game")
    assert fn is not None, "library_mixin.refetch_game does not exist"
    snaps = _calls_named(fn, "snapshot_for_restore")
    assert snaps, "library_mixin.refetch_game must call snapshot_for_restore before deleting"
    removes = _unlink_calls(fn)
    assert removes, "library_mixin.refetch_game must delete files (os.remove/os.unlink)"
    assert snaps[0].lineno < min(r.lineno for r in removes), (
        "snapshot_for_restore must run BEFORE the delete loop (a reversed order "
        "snapshots an empty folder)")


def test_wiring_begin_restore_run_before_fetch_loop():
    """begin_restore_run must run before the first For in _run_fetch_body."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "_run_fetch_body")
    assert fn is not None, "fetch_mixin._run_fetch_body does not exist"
    begins = _calls_named(fn, "begin_restore_run")
    assert begins, "fetch_mixin._run_fetch_body must call begin_restore_run"
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert loops, "fetch_mixin._run_fetch_body must contain a For loop"
    first_for = min(loops, key=lambda n: n.lineno)
    assert begins[0].lineno < first_for.lineno, (
        "begin_restore_run must run BEFORE the fetch loop")


def test_wiring_seal_restore_point_after_fetch_loop():
    """seal_restore_point must run after the same For loop the fetch iterates."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "_run_fetch_body")
    assert fn is not None, "fetch_mixin._run_fetch_body does not exist"
    seals = _calls_named(fn, "seal_restore_point")
    assert seals, "fetch_mixin._run_fetch_body must call seal_restore_point after the loop"
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert loops, "fetch_mixin._run_fetch_body must contain a For loop"
    first_for = min(loops, key=lambda n: n.lineno)
    # Containment again: seal_restore_point as the FIRST statement of the loop
    # body satisfies `lineno >` while leaving the last game's entry unsealed.
    assert not any(_contains(first_for, sl) for sl in seals), (
        "seal_restore_point must not sit INSIDE the fetch loop")
    assert seals[0].lineno > first_for.end_lineno, (
        "seal_restore_point must run AFTER the whole fetch loop")


def test_wiring_undo_fetch_delegates_to_undo_restore_point():
    """undo_fetch must delegate to undo_restore_point."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "undo_fetch")
    assert fn is not None, "fetch_mixin.undo_fetch does not exist"
    calls = _calls_named(fn, "undo_restore_point")
    assert calls, "fetch_mixin.undo_fetch must call undo_restore_point"


def test_wiring_undo_fetch_has_no_os_remove_copy():
    """Deletion moved into find_games.undo_restore_point — a leftover os.remove
    here would double-delete."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "undo_fetch")
    assert fn is not None, "fetch_mixin.undo_fetch does not exist"
    hits = _unlink_calls(fn)
    assert not hits, (
        "fetch_mixin.undo_fetch must not delete files (os.remove/os.unlink) — "
        "deletion moved into find_games.undo_restore_point")


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


def test_wiring_begin_restore_run_after_needs_art_early_return():
    """begin_restore_run must sit AFTER the 'if not needs_art:' early return —
    a no-op fetch must not be able to destroy the previous restore point."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "_run_fetch_body")
    assert fn is not None, "fetch_mixin._run_fetch_body does not exist"
    begins = _calls_named(fn, "begin_restore_run")
    assert begins, "fetch_mixin._run_fetch_body must call begin_restore_run"
    early = None
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and _references_name(n.test, "needs_art"):
            if early is None or n.lineno < early.lineno:
                early = n
    assert early is not None, "fetch_mixin._run_fetch_body must contain the 'if not needs_art:' early return"
    # Containment, not line order: a begin_restore_run() placed INSIDE the
    # `if not needs_art:` block also satisfies `lineno >`, and that is exactly
    # the failure this gate exists to catch — the no-op fetch still drops every
    # sealed entry and erases the operator's Undo.
    assert not any(_contains(early, b) for b in begins), (
        "begin_restore_run must not sit INSIDE the needs_art early-return block")
    assert begins[0].lineno > early.end_lineno, (
        "begin_restore_run must run AFTER the whole needs_art early return, or a "
        "no-op fetch destroys the previous restore point")


def test_wiring_fetch_log_reports_per_game_elapsed():
    """Per-game timing: the 'Artwork saved for …' log line must interpolate an
    elapsed value, so 'incredibly slow' becomes a number the operator can quote."""
    tree = _module("fetch_mixin.py")
    fn = _func(tree, "_run_fetch_body")
    assert fn is not None, "fetch_mixin._run_fetch_body does not exist"
    target = None
    for n in ast.walk(fn):
        if isinstance(n, ast.JoinedStr):
            literal = "".join(
                v.value for v in n.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if "Artwork saved for" in literal:
                target = n
                break
    assert target is not None, (
        "fetch_mixin._run_fetch_body must log 'Artwork saved for ...' as an f-string")
    refs = []
    for v in target.values:
        if isinstance(v, ast.FormattedValue):
            for sub in ast.walk(v):
                if isinstance(sub, ast.Name) and "elapsed" in sub.id:
                    refs.append(sub.id)
    assert refs, (
        "the 'Artwork saved for' log line must interpolate a per-game elapsed "
        "value (e.g. {elapsed:.1f}s), got formatted values: %s"
        % [v.lineno for v in target.values])


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
    assert has, "app.py must initialise it from fg.has_restore_point()"


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
