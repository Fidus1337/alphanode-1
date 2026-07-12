#!/usr/bin/env bash
# Build a native .deb for Ubuntu/Debian from the AlphaNode PyInstaller build.
# Run from the repository root:  bash packaging/build_deb.sh   [version]
#
# Installs the application into /opt/alphanode, adds a menu entry + icon and an `alphanode` command
# in the terminal (including CLI: `alphanode --role cli top`). Python is NOT needed on the machine.
# User installation:  sudo apt install ./alphanode_<ver>_amd64.deb   (or double-click)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(dirname "$HERE")"
PY="${PYTHON:-$PROJ/.venv/bin/python}"
VER="${1:-1.0.0}"
ARCH="amd64"
ONEDIR="$HERE/dist/AlphaNode"                     # PyInstaller result (onedir)
PKG="$HERE/deb/alphanode_${VER}_${ARCH}"
OUT="$HERE/dist/alphanode_${VER}_${ARCH}.deb"

echo "== [1/4] Checking the PyInstaller build =="
if [ ! -x "$ONEDIR/AlphaNode" ]; then
  echo "  onedir not found — building (PyInstaller)…"
  if ! "$PY" -c "import PyInstaller" 2>/dev/null; then "$PY" -m pip install --quiet --upgrade pyinstaller; fi
  "$PY" "$HERE/make_icon.py"
  "$PY" -m PyInstaller --noconfirm --clean --distpath "$HERE/dist" --workpath "$HERE/build" "$HERE/AlphaNode.spec"
fi
[ -f "$HERE/alphanode-256.png" ] || "$PY" "$HERE/make_icon.py"

echo "== [2/4] Building the package tree =="
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/alphanode" "$PKG/usr/bin" \
         "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/256x256/apps"

cp -a "$ONEDIR/." "$PKG/opt/alphanode/"

# terminal command -> launches the binary (GUI by default; supports --role cli …)
cat > "$PKG/usr/bin/alphanode" <<'EOF'
#!/bin/sh
exec /opt/alphanode/AlphaNode "$@"
EOF
chmod 0755 "$PKG/usr/bin/alphanode"

cp "$HERE/alphanode-256.png" "$PKG/usr/share/icons/hicolor/256x256/apps/alphanode.png"
cat > "$PKG/usr/share/applications/alphanode.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=AlphaNode
GenericName=Alpha strategy search
Comment=Evolutionary search for trading strategies (GP) with a live status panel
Exec=alphanode
Icon=alphanode
Categories=Finance;Science;
Terminal=false
StartupNotify=true
EOF

SIZE_KB="$(du -ks "$PKG/opt" "$PKG/usr" | awk '{s+=$1} END{print s}')"
cat > "$PKG/DEBIAN/control" <<EOF
Package: alphanode
Version: ${VER}
Section: science
Priority: optional
Architecture: ${ARCH}
Depends: libc6
Installed-Size: ${SIZE_KB}
Maintainer: AlphaNode <yurbusht@gmail.com>
Description: Evolutionary search for trading strategies (headless node + GUI)
 AlphaNode searches for robust alpha formulas via genetic programming and accumulates them
 in a library. It has a desktop GUI and a CLI (alphanode --role cli …). Python and dependencies
 are bundled — nothing needs to be installed. Data is written to ~/.local/share/AlphaNode.
EOF

# refresh icon/desktop caches after install/removal (not critical, but tidy)
cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications || true; fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true; fi
EOF
cp "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"
chmod 0755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"

echo "== [3/4] Packaging the .deb =="
dpkg-deb --build --root-owner-group "$PKG" "$OUT"

echo "== [4/4] Done =="
ls -lh "$OUT"
echo "Install:   sudo apt install \"$OUT\"      (or double-click in your file manager)"
echo "Run:       applications menu -> AlphaNode   |   in the terminal: alphanode"
echo "Remove:    sudo apt remove alphanode"
