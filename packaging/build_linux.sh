#!/usr/bin/env bash
# Build the AlphaNode desktop application for Linux into a single portable file (.AppImage).
# Run from the repository root:  bash packaging/build_linux.sh
#
# Result:  packaging/dist/AlphaNode-x86_64.AppImage  — works on any modern Linux
# (Ubuntu/Debian/Fedora/…), python is NOT needed on the machine: the interpreter and all dependencies are inside.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(dirname "$HERE")"
PY="${PYTHON:-$PROJ/.venv/bin/python}"
ARCH="${ARCH:-x86_64}"
APPNAME="AlphaNode"
DIST="$HERE/dist"
WORK="$HERE/build"
APPDIR="$HERE/${APPNAME}.AppDir"

echo "== [1/6] Checking PyInstaller =="
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "  installing pyinstaller into venv ($PY)…"
  "$PY" -m pip install --quiet --upgrade pyinstaller
fi
"$PY" -c "import PyInstaller; print('  PyInstaller', PyInstaller.__version__)"

echo "== [2/6] Icon =="
"$PY" "$HERE/make_icon.py"

echo "== [3/6] PyInstaller (onedir) =="
rm -rf "$DIST" "$WORK" "$APPDIR"
"$PY" -m PyInstaller --noconfirm --clean \
  --distpath "$DIST" --workpath "$WORK" \
  "$HERE/${APPNAME}.spec"

echo "== [4/6] Building AppDir =="
mkdir -p "$APPDIR/usr/bin"
cp -a "$DIST/${APPNAME}/." "$APPDIR/usr/bin/"
cp "$HERE/alphanode.png" "$APPDIR/alphanode.png"
cp "$HERE/alphanode.desktop" "$APPDIR/alphanode.desktop"
# some launchers also expect the desktop file/icon in usr/share
mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp "$HERE/alphanode.desktop" "$APPDIR/usr/share/applications/alphanode.desktop"
cp "$HERE/alphanode-256.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/alphanode.png"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="$HERE/usr/bin:$PATH"
exec "$HERE/usr/bin/AlphaNode" "$@"
EOF
chmod +x "$APPDIR/AppRun"

echo "== [5/6] Fetching appimagetool =="
TOOL="$HERE/appimagetool-${ARCH}.AppImage"
if [ ! -x "$TOOL" ]; then
  URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  echo "  downloading $URL"
  curl -fsSL -o "$TOOL" "$URL"
  chmod +x "$TOOL"
fi

echo "== [6/6] Building AppImage =="
OUT="$DIST/${APPNAME}-${ARCH}.AppImage"
# FUSE may be unavailable (containers) — then extract-and-run
if ! ARCH="$ARCH" "$TOOL" "$APPDIR" "$OUT" 2>/dev/null; then
  ARCH="$ARCH" "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
fi

echo
echo "✓ Done: $OUT"
ls -lh "$OUT"
echo "Run:  \"$OUT\"      (or double-click in your file manager)"
