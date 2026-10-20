"""Suite-wide sandbox HOME, its G-HOME guard, and the no-network guard.

Sandbox HOME: importing appfiles binds every app path (appfiles.DATA_FOLDER:
~/.config/NonSteamScraper, or %APPDATA%\\NonSteamScraper on Windows) to HOME /
APPDATA at import time, so HOME, APPDATA and XDG_CONFIG_HOME must point inside a
throw-away folder BEFORE any test module imports it. pytest loads this conftest before it collects the test modules, so this is the one place that creates it — and the one place
that removes it again when the session ends (pytest_unconfigure runs after a
normal run, a failed run and a --collect-only run alike). The real HOME is
never read or written.

G-HOME: a test must write into its own tmp_path, never into the sandbox HOME.
A test that patches a path on the wrong module (not the one that owns it)
silently falls back to the HOME-derived default and may still pass; the guard turns that into a failure naming what appeared.

No-network: any test that attempts a real outbound socket connection fails with
an AssertionError naming the address — the backstop for every HTTP seam patch.

Must NOT import appfiles or any backend module (they would bind before the
sandbox exists).
"""

import os
import shutil
import socket
import tempfile

import pytest

SANDBOX_HOME = tempfile.mkdtemp(prefix="steamart_test_home_")
os.environ["HOME"] = SANDBOX_HOME
os.environ["USERPROFILE"] = SANDBOX_HOME   # Windows-style home resolution
# The data folder's base on Windows; on a Windows runner the real one would be
# the user's own %APPDATA%.
os.environ["APPDATA"] = os.path.join(SANDBOX_HOME, "AppData", "Roaming")
# Not read by the app (Q3), sandboxed anyway so nothing can follow it out.
os.environ["XDG_CONFIG_HOME"] = os.path.join(SANDBOX_HOME, ".config")


def pytest_unconfigure(config):
    shutil.rmtree(SANDBOX_HOME, ignore_errors=True)


def _sandbox_listing():
    """Every path under the sandbox HOME with its size and mtime."""
    found = {}
    for root, dirs, files in os.walk(SANDBOX_HOME):
        for name in dirs + files:
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            found[os.path.relpath(p, SANDBOX_HOME)] = (st.st_size, st.st_mtime_ns)
    return found


@pytest.fixture(autouse=True)
def _sandbox_home_untouched():
    """G-HOME: fail the test that changed anything under the sandbox HOME, then
    empty it so the next test starts clean."""
    before = _sandbox_listing()
    yield
    after = _sandbox_listing()
    if after != before:
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        for name in os.listdir(SANDBOX_HOME):
            p = os.path.join(SANDBOX_HOME, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        pytest.fail("G-HOME: the test wrote into the sandbox HOME instead of its "
                    "tmp_path (patch the path on the module that owns it): %s" % changed)


@pytest.fixture(autouse=True)
def _no_network_guard(monkeypatch):
    """Make any real socket connect (and connect_ex) fail the test that caused
    it, naming the address it tried to reach."""

    def _blocked(self, address, *args, **kwargs):
        raise AssertionError(
            "no-network guard: test attempted a real socket connection to %r — "
            "patch the HTTP seam instead of reaching the network" % (address,))

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
