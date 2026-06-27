"""Shared constants and helpers used across app.py and its mixin/window modules.

Kept in a tiny, dependency-light module (only os/sys) so every other module —
the mixins and the extracted window classes — can import VERSION, the resource
locator, and the first-run marker WITHOUT importing app.py (which would create a
circular import, since app.py imports those modules in turn).
"""

import os
import sys

VERSION = "1.4.1"

FIRST_RUN_FILE = os.path.expanduser("~/.steamart_firstrun")


def resource_path(relative):
    """Return the absolute path to a bundled resource, working both when running
    as a script and when packaged by PyInstaller (which unpacks to sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)
