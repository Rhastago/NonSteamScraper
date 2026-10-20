#!/usr/bin/env bash
# Build a self-contained Windows build bundle for manual testing on a Windows box.
# Run this on the Deck; it produces a .zip in dist/ containing the current source,
# pre-fetched Windows wheels (offline install), requirements*.txt (online fallback),
# and BUILD.txt with the exact Git Bash commands.
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
[ -x venv/bin/python ] && PY=venv/bin/python

VER=$("$PY" -c "import re,sys;print(re.search(r'VERSION = \"([^\"]+)\"',open('appcommon.py').read()).group(1))")
SHA=$(git rev-parse --short HEAD 2>/dev/null || echo nogit)
# Output to winbuild/ (NOT dist/) so the Linux build's `rm -rf dist` can't wipe it.
OUTDIR=winbuild
STAGE=$(mktemp -d)
APP="$STAGE/NonSteamScraper"
mkdir -p "$APP" "$OUTDIR"

echo "Staging source (working tree)…"
# Everything a Windows build needs, taken from the working tree (so uncommitted
# changes are included). Every *.py at the repo root is an app module: a fixed
# list once missed a new module and broke only the Windows build.
for item in *.py \
            requirements.txt requirements-build.txt requirements-dev.txt constraints.txt tests \
            icon.png icon.ico \
            build_windows.sh assets README.md CHANGELOG.md LICENSE; do
    [ -e "$item" ] && cp -r "$item" "$APP/"
done
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "Fetching Windows wheels (cp313, pinned by constraints.txt)…"
"$PY" -m pip download -r requirements-build.txt -r requirements-dev.txt -c constraints.txt \
    --platform win_amd64 --python-version 3.13 --only-binary=:all: -d "$APP/wheels" >/dev/null
# pip judges the sys_platform markers by THIS machine, so the Windows-only
# dependencies are never pulled in above; fetch them by their exact pins.
WIN_ONLY=$(grep 'sys_platform == "win32"' constraints.txt | cut -d';' -f1)
# shellcheck disable=SC2086
"$PY" -m pip download $WIN_ONLY --no-deps --platform win_amd64 --python-version 3.13 \
    --only-binary=:all: -d "$APP/wheels" >/dev/null

cat > "$APP/BUILD.txt" <<'TXT'
NonSteamScraper — Windows test build (run these in Git Bash)
============================================================

1. Open Git Bash and cd into this extracted folder:
       cd /path/to/NonSteamScraper

2. Create and activate a virtual environment:
       python -m venv venv
       source venv/Scripts/activate

3. Install dependencies (the app's and PyInstaller) at the exact versions in
   constraints.txt — tries the bundled offline wheels first, falls back online:
       pip install --no-index --find-links wheels -r requirements-build.txt -c constraints.txt \
         || pip install -r requirements-build.txt -c constraints.txt

   (The offline wheels target Python 3.13, the version CI builds with. On a
    different Python version the offline step fails and pip installs the same
    pinned versions from the internet.)

4. Run the test suite (the same one CI runs before a release):
       pip install --no-index --find-links wheels -r requirements-dev.txt -c constraints.txt \
         || pip install -r requirements-dev.txt -c constraints.txt
       python -m pytest -q

5. Build the executable:
       ./build_windows.sh

6. Result:  dist/NonSteamScraper.exe   (run it to manually test)
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
