"""Shared constants and helpers used across app.py and its mixin/window modules.

Kept small and tkinter-free (only stdlib: os/sys/subprocess/webbrowser) so every
other module — the mixins and the extracted window classes — can import VERSION,
the resource locator, the first-run marker, and the URL opener WITHOUT importing
app.py (which would create a circular import, since app.py imports those modules
in turn).
"""

import os
import sys
import subprocess
import webbrowser

VERSION = "1.4.3"

FIRST_RUN_FILE = os.path.expanduser("~/.steamart_firstrun")


def resource_path(relative):
    """Return the absolute path to a bundled resource, working both when running
    as a script and when packaged by PyInstaller (which unpacks to sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def _browser_env():
    """Environment for spawning an external browser, or None to inherit the current
    one. In a PyInstaller bundle the bootloader points LD_LIBRARY_PATH (DYLD on mac)
    at the bundled libs; a spawned browser inherits that and fails to load its own
    system libraries — which is why webbrowser.open() silently dies on the packaged
    Linux/Steam Deck build. PyInstaller saves the pre-launch values as *_ORIG, so
    restore those (or drop the injected var) for the child process."""
    if not getattr(sys, "frozen", False):
        return None  # running from source — the current environment is already clean
    env = dict(os.environ)
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        orig = env.pop(var + "_ORIG", None)
        if orig is None:
            env.pop(var, None)  # bootloader injected it with no saved original
        else:
            env[var] = orig     # restore the pre-bundle value for the browser
    return env


def open_url(url):
    """Open a URL in the user's default browser, reliably from a packaged build.

    webbrowser.open() works from source but fails in a PyInstaller --windowed binary
    (no stdio for its subprocess controllers, and on Linux the browser inherits the
    bundle's LD_LIBRARY_PATH). So spawn the OS opener directly with a cleaned
    environment, and only fall back to webbrowser if that raises. Never raises."""
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]  # Windows-only, env-agnostic
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, url], env=_browser_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            pass
