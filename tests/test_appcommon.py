"""S1 gates — pre_bundle_env / relaunch_env environment normalization (T1).

Target: `appcommon.pre_bundle_env(env)` and `appcommon.relaunch_env(env)` do not
exist yet. Both take a dict and return a NEW dict; neither mutates its argument
nor reads os.environ. Every gate below leads with a presence assertion carrying a
message, so an absent implementation fails by ASSERTION, never by AttributeError.

The measured PyInstaller 6.20 bootloader behaviour (SPEC.md S0, measured
2026-08-17) is: the bootloader PREPENDS its unpack dir to any inherited
LD_LIBRARY_PATH and sets LD_LIBRARY_PATH_ORIG to what it inherited (unsetting it
when nothing was inherited). Without the S1 fix, each relaunch leaves the
previous bundle's dir in LD_LIBRARY_PATH_ORIG, and _browser_env() then feeds
that stale bundle dir to the spawned browser — killing every in-app link.
"""

import os
import socket
import sys

import pytest

# appcommon is tkinter-free; the repo root must be on sys.path for the plain
# `pytest tests/test_appcommon.py` invocation (tests/ has no __init__.py, so
# pytest inserts tests/, not the repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import appcommon  # noqa: E402


def _bootloader(env, mei):
    """What PyInstaller 6.20 does at startup. MEASURED 2026-08-17, SPEC.md S0:
    it PREPENDS its unpack dir to any inherited LD_LIBRARY_PATH, and sets
    LD_LIBRARY_PATH_ORIG to what it inherited (unsetting it when nothing was
    inherited)."""
    out = dict(env)
    inherited = out.get("LD_LIBRARY_PATH")
    if inherited:
        out["LD_LIBRARY_PATH_ORIG"] = inherited
        out["LD_LIBRARY_PATH"] = mei + ":" + inherited
    else:
        out.pop("LD_LIBRARY_PATH_ORIG", None)
        out["LD_LIBRARY_PATH"] = mei
    out["_MEIPASS"] = mei
    return out


def test_pre_bundle_env_drops_ld_library_path_when_no_orig():
    """Fresh-launch shape: the bundle injected LD_LIBRARY_PATH but there was no
    original — the browser environment must not carry it at all."""
    assert hasattr(appcommon, "pre_bundle_env"), "appcommon.pre_bundle_env does not exist yet"
    out = appcommon.pre_bundle_env({"LD_LIBRARY_PATH": "/tmp/_MEI0", "OTHER": "x"})
    assert "LD_LIBRARY_PATH" not in out
    assert out.get("OTHER") == "x"


def test_pre_bundle_env_restores_orig_ld_library_path():
    """LD_LIBRARY_PATH="/new:/old" with LD_LIBRARY_PATH_ORIG="/old" → the browser
    gets "/old" (the pre-bundle value), not the bundle's accumulated path."""
    assert hasattr(appcommon, "pre_bundle_env"), "appcommon.pre_bundle_env does not exist yet"
    out = appcommon.pre_bundle_env(
        {"LD_LIBRARY_PATH": "/new:/old", "LD_LIBRARY_PATH_ORIG": "/old"})
    assert out["LD_LIBRARY_PATH"] == "/old"


def test_pre_bundle_env_never_leaves_an_orig_key():
    """No *_ORIG key may survive in the result, in the restore case or the
    fresh-launch case alike."""
    assert hasattr(appcommon, "pre_bundle_env"), "appcommon.pre_bundle_env does not exist yet"
    for env in ({"LD_LIBRARY_PATH": "/new", "LD_LIBRARY_PATH_ORIG": "/old"},
                {"LD_LIBRARY_PATH": "/new"},
                {"DYLD_LIBRARY_PATH": "/a", "DYLD_LIBRARY_PATH_ORIG": "/a"}):
        out = appcommon.pre_bundle_env(dict(env))
        assert not [k for k in out if k.endswith("_ORIG")], (
            "pre_bundle_env must strip every *_ORIG key, got %r" % out)


def test_pre_bundle_env_dyld_library_path_matches_ld_handling():
    """DYLD_LIBRARY_PATH is handled exactly like LD_LIBRARY_PATH."""
    assert hasattr(appcommon, "pre_bundle_env"), "appcommon.pre_bundle_env does not exist yet"
    out = appcommon.pre_bundle_env(
        {"DYLD_LIBRARY_PATH": "/new:/old", "DYLD_LIBRARY_PATH_ORIG": "/old"})
    assert out["DYLD_LIBRARY_PATH"] == "/old"
    fresh = appcommon.pre_bundle_env({"DYLD_LIBRARY_PATH": "/new"})
    assert "DYLD_LIBRARY_PATH" not in fresh


def test_relaunch_env_strips_mei_and_normalizes_library_path():
    """relaunch_env drops every _MEI*/_PYI* key AND applies the same library-path
    normalization, leaving all unrelated keys untouched."""
    assert hasattr(appcommon, "relaunch_env"), "appcommon.relaunch_env does not exist yet"
    env = {"_MEIPASS": "/tmp/_MEI0", "_PYI_LAUNCHER_NAME": "x", "_MEI5432": "y",
           "LD_LIBRARY_PATH": "/new:/old", "LD_LIBRARY_PATH_ORIG": "/old",
           "HOME": "/home/u", "PATH": "/bin"}
    out = appcommon.relaunch_env(env)
    assert "_MEIPASS" not in out
    assert "_PYI_LAUNCHER_NAME" not in out
    assert "_MEI5432" not in out
    assert out["LD_LIBRARY_PATH"] == "/old"
    assert "LD_LIBRARY_PATH_ORIG" not in out
    assert out["HOME"] == "/home/u"
    assert out["PATH"] == "/bin"


def test_relaunch_loop_never_accumulates_a_bundle_library_path():
    """THE regression this whole change exists for. Two consecutive relaunches
    through the measured bootloader must present a fresh-launch environment:
    pre_bundle_env of generation 1 (and 2) contains NO LD_LIBRARY_PATH at all.
    Today's code fails this: the generation-1 browser environment gets
    LD_LIBRARY_PATH == "/tmp/_MEI0"."""
    assert hasattr(appcommon, "relaunch_env"), "appcommon.relaunch_env does not exist yet"
    assert hasattr(appcommon, "pre_bundle_env"), "appcommon.pre_bundle_env does not exist yet"

    gen = _bootloader({}, "/tmp/_MEI0")                                   # generation 0
    gen = _bootloader(appcommon.relaunch_env(gen), "/tmp/_MEI1")          # generation 1
    browser_env = appcommon.pre_bundle_env(gen)
    assert "LD_LIBRARY_PATH" not in browser_env, (
        "generation-1 browser env must not carry a bundle library path, got %r" % browser_env)

    gen = _bootloader(appcommon.relaunch_env(gen), "/tmp/_MEI2")          # generation 2
    browser_env = appcommon.pre_bundle_env(gen)
    assert "LD_LIBRARY_PATH" not in browser_env, (
        "generation-2 browser env must not carry a bundle library path, got %r" % browser_env)


def test_env_helpers_return_new_dict_and_do_not_mutate_input():
    """Both helpers return a NEW dict and never mutate their argument."""
    assert hasattr(appcommon, "pre_bundle_env"), "appcommon.pre_bundle_env does not exist yet"
    assert hasattr(appcommon, "relaunch_env"), "appcommon.relaunch_env does not exist yet"
    env = {"LD_LIBRARY_PATH": "/new:/old", "LD_LIBRARY_PATH_ORIG": "/old", "KEEP": 1}
    before = dict(env)
    out1 = appcommon.pre_bundle_env(env)
    out2 = appcommon.relaunch_env(env)
    assert env == before
    assert out1 is not env
    assert out2 is not env


def test_open_url_still_exists():
    """open_url is the single funnel for every in-app link; it must not have
    been renamed or removed by the env refactor."""
    assert callable(appcommon.open_url), "appcommon.open_url has been removed"


def test_browser_env_returns_none_when_running_from_source(monkeypatch):
    """Running from source (no PyInstaller) must be unchanged: _browser_env()
    returns None, i.e. the child inherits the current environment."""
    monkeypatch.delattr(appcommon.sys, "frozen", raising=False)
    assert appcommon._browser_env() is None


def test_no_network_guard_blocks_a_real_socket_connect():
    """Proof the tests/conftest.py no-network guard actually patches something:
    a real socket.connect must fail with the guard's AssertionError. If this
    passes, the whole S2 migration has its backstop."""
    s = socket.socket()
    try:
        with pytest.raises(AssertionError, match="no-network guard"):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()
