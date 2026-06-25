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
fi

rm -rf build dist

pyinstaller --onefile --windowed --name NonSteamScraper \
    --add-data "icon.png:." \
    --hidden-import PIL._tkinter_finder \
    --hidden-import PIL.PngImagePlugin \
    --hidden-import PIL.JpegImagePlugin \
    --hidden-import PIL.WebPImagePlugin \
    --hidden-import PIL.GifImagePlugin \
    --hidden-import PIL.IcoImagePlugin \
    --collect-all pillow app.py

echo
echo "Done. Binary is at: dist/NonSteamScraper"
echo "Rename to NonSteamScraper-linux before uploading to a GitHub release."
