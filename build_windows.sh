#!/usr/bin/env bash
# Build the standalone Windows .exe for NonSteamScraper. Run from Git Bash.
#
# Usage:  ./build_windows.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ -f venv/Scripts/activate ]; then
    # shellcheck disable=SC1091
    source venv/Scripts/activate
fi

# Generate icon.ico from icon.png if it doesn't exist yet.
if [ ! -f icon.ico ]; then
    python -c "from PIL import Image; Image.open('icon.png').save('icon.ico')"
fi

rm -rf build dist

pyinstaller --onefile --windowed --name NonSteamScraper \
    --icon icon.ico \
    --add-data "icon.png;." \
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
    --collect-all pillow \
    --collect-all multiprocessing app.py

echo
echo "Done. Binary is at: dist/NonSteamScraper.exe"
echo "Rename to NonSteamScraper-windows.exe before uploading to a GitHub release."
