#!/usr/bin/env bash
# Build a self-contained Windows build bundle for manual testing on a Windows box.
# Run this on the Deck; it produces a .zip in dist/ containing the current source,
# pre-fetched Windows wheels (offline install), requirements.txt (online fallback),
# and BUILD.txt with the exact Git Bash commands.
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
[ -x venv/bin/python ] && PY=venv/bin/python

VER=$("$PY" -c "import re,sys;print(re.search(r'VERSION = \"([^\"]+)\"',open('app.py').read()).group(1))")
SHA=$(git rev-parse --short HEAD 2>/dev/null || echo nogit)
# Output to winbuild/ (NOT dist/) so the Linux build's `rm -rf dist` can't wipe it.
OUTDIR=winbuild
STAGE=$(mktemp -d)
APP="$STAGE/NonSteamScraper"
mkdir -p "$APP" "$OUTDIR"

echo "Staging source (working tree)…"
# Explicit list of everything a Windows build needs, taken from the working tree
# (so uncommitted changes are included).
for item in app.py find_games.py requirements.txt icon.png icon.ico \
            build_windows.sh assets README.md CHANGELOG.md LICENSE; do
    [ -e "$item" ] && cp -r "$item" "$APP/"
done

echo "Fetching Windows wheels (cp312)…"
"$PY" -m pip download -r requirements.txt --platform win_amd64 \
    --python-version 3.12 --only-binary=:all: -d "$APP/wheels" >/dev/null

cat > "$APP/BUILD.txt" <<'TXT'
NonSteamScraper — Windows test build (run these in Git Bash)
============================================================

1. Open Git Bash and cd into this extracted folder:
       cd /path/to/NonSteamScraper

2. Create and activate a virtual environment:
       python -m venv venv
       source venv/Scripts/activate

3. Install dependencies — tries the bundled offline wheels first, falls back online:
       pip install --no-index --find-links wheels -r requirements.txt \
         || pip install -r requirements.txt

   (The offline wheels target Python 3.12. On a different Python version the
    offline step is skipped automatically and pip installs from the internet.)

4. Build the executable:
       ./build_windows.sh

5. Result:  dist/NonSteamScraper.exe   (run it to manually test)
TXT

echo "Zipping…"
OUT="${OUTDIR}/NonSteamScraper-winbuild-v${VER}-${SHA}"
"$PY" - "$STAGE" "$OUT" <<'PYEOF'
import shutil, sys
stage, out = sys.argv[1], sys.argv[2]
shutil.make_archive(out, "zip", root_dir=stage, base_dir="NonSteamScraper")
PYEOF

rm -rf "$STAGE"
echo "Done: ${OUT}.zip"
