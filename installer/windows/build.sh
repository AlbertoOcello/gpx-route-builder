#!/bin/bash
# Costruisce GPX-Route-Builder-Setup.exe
# Requisiti: makensis (brew install nsis), magick (brew install imagemagick)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$REPO/dist"
WIN_DIR="$REPO/installer/windows"
EXE_OUT="$DIST/GPX-Route-Builder-Setup.exe"

mkdir -p "$DIST"

echo "==> Conversione icona PNG → .ico"
magick "$REPO/Icona_RB.png" \
    \( -clone 0 -resize 256x256 \) \
    \( -clone 0 -resize 128x128 \) \
    \( -clone 0 -resize 64x64  \) \
    \( -clone 0 -resize 48x48  \) \
    \( -clone 0 -resize 32x32  \) \
    \( -clone 0 -resize 16x16  \) \
    -delete 0 \
    "$WIN_DIR/icon.ico"
echo "   icon.ico creato"

echo "==> Copia launcher.ps1 nella dir NSIS"
# launcher.ps1 è già in windows/, nulla da copiare

echo "==> Compilazione NSIS → .exe"
cd "$WIN_DIR"
makensis -V2 installer.nsi
mv "$WIN_DIR/GPX-Route-Builder-Setup.exe" "$EXE_OUT"

echo ""
echo "✅  $EXE_OUT"
ls -lh "$EXE_OUT"
