#!/usr/bin/env bash
# Build the standalone Linux binary for NonSteamScraper.
#
# IMPORTANT: run this from Konsole (a native terminal), NOT the VSCode integrated
# terminal. Inside VSCode's Flatpak sandbox tkinter is broken, which produces a
# non-working binary.
#
# Usage:  ./build_linux.sh
set -euo pipefail
cd "$(dirname "$0")"

# Use the project venv if present, otherwise fall back to whatever pyinstaller is on PATH.
if [ -f venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
    # The exact versions the release was tested with (constraints.txt). A no-op
    # when the venv already matches them.
    python -m pip install -q -r requirements-build.txt -c constraints.txt
fi

rm -rf build dist

# The flag list below is the SAME as build_windows.sh's (only the --add-data
# separator differs, ":" here and ";" there, and Windows adds --icon). Change both.
pyinstaller --onefile --windowed --name NonSteamScraper \
    --add-data "icon.png:." \
    --add-data "assets:assets" \
    --hidden-import PIL._tkinter_finder \
    --hidden-import PIL.PngImagePlugin \
    --hidden-import PIL.JpegImagePlugin \
    --hidden-import PIL.WebPImagePlugin \
    --hidden-import PIL.GifImagePlugin \
    --hidden-import PIL.IcoImagePlugin \
    --hidden-import requests \
    --hidden-import vdf \
    --hidden-import psutil \
    --hidden-import _socket \
    --hidden-import socket \
    app.py

echo
echo "Done. Binary is at: dist/NonSteamScraper"
echo "Rename to NonSteamScraper-linux before uploading to a GitHub release."
