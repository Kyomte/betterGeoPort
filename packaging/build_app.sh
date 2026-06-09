#!/bin/bash
# Build GeoPort.app from source. Run from the project root with the venv active.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
OUT="${1:-$HERE/GeoPort.app}"

echo "[1/3] PyInstaller build..."
pyinstaller --noconfirm --clean GeoPort.spec

echo "[2/3] Assembling $OUT ..."
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp packaging/Info.plist "$OUT/Contents/Info.plist"
cp packaging/launcher   "$OUT/Contents/MacOS/launcher"; chmod +x "$OUT/Contents/MacOS/launcher"
# App icon: reuse the original if present, else skip.
[ -f packaging/AppIcon.icns ] && cp packaging/AppIcon.icns "$OUT/Contents/Resources/AppIcon.icns"
cp -R dist/GeoPort "$OUT/Contents/Resources/GeoPort"

echo "[3/3] Ad-hoc codesign (required on Apple Silicon)..."
codesign --force --deep --sign - "$OUT"
codesign --verify --verbose=1 "$OUT"
echo "Done: $OUT"
