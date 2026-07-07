#!/bin/bash
# Costruisce GPX-Route-Builder-Mac.dmg
# Requisiti: create-dmg (brew install create-dmg), sips, iconutil (macOS built-in)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$REPO/dist"
BUILD="$REPO/installer/mac/build"
APP_NAME="GPX Route Builder"
APP_BUNDLE="$BUILD/${APP_NAME}.app"
DMG_OUT="$DIST/GPX-Route-Builder-Mac.dmg"
SIGN_ID="Developer ID Application: ALBERTO OCELLO (269RJ27L8F)"

mkdir -p "$DIST" "$BUILD"

echo "==> Conversione icona PNG → .icns"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
SRCPNG="$REPO/Icona_RB.png"

for size in 16 32 64 128 256 512; do
    sips -z $size $size "$SRCPNG" --out "$ICONSET/icon_${size}x${size}.png"      >/dev/null
    sips -z $((size*2)) $((size*2)) "$SRCPNG" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns -o "$BUILD/AppIcon.icns" "$ICONSET"
rm -rf "$ICONSET"
echo "   AppIcon.icns creato"

echo "==> Costruzione .app bundle"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Info.plist
cat > "$APP_BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>             <string>GPX Route Builder</string>
    <key>CFBundleDisplayName</key>      <string>GPX Route Builder</string>
    <key>CFBundleIdentifier</key>       <string>com.albertoocello.gpxroutebuilder</string>
    <key>CFBundleVersion</key>          <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>CFBundleExecutable</key>       <string>GPX Route Builder</string>
    <key>CFBundleIconFile</key>         <string>AppIcon</string>
    <key>NSHighResolutionCapable</key>  <true/>
    <key>LSMinimumSystemVersion</key>   <string>11.0</string>
    <key>NSAppleEventsUsageDescription</key>
        <string>GPX Route Builder usa Apple Events per mostrare dialoghi e notifiche.</string>
</dict>
</plist>
PLIST

# Eseguibile = launcher.sh copiato con il nome giusto
cp "$REPO/installer/mac/launcher.sh" "$APP_BUNDLE/Contents/MacOS/GPX Route Builder"
chmod +x "$APP_BUNDLE/Contents/MacOS/GPX Route Builder"

# Icona
cp "$BUILD/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"

echo "   .app bundle assemblato"

echo "==> Firma .app con Developer ID (richiede keychain sbloccato)"
# Se il prompt keychain non appare, sblocca prima con:
#   security unlock-keychain ~/Library/Keychains/login.keychain-db
codesign --deep --force --verify \
    --sign "$SIGN_ID" \
    --options runtime \
    --entitlements "$REPO/installer/mac/entitlements.plist" \
    "$APP_BUNDLE" && echo "   .app firmato" || echo "⚠️  firma saltata — sblocca il keychain e rifai"

echo "==> Sfondo DMG"
# Crea un semplice sfondo 800x450 con testo
magick -size 800x450 gradient:"#e8eef5-#d0daea" \
    -fill "#1d3557" -font Helvetica-Bold -pointsize 26 \
    -gravity North -annotate +0+60 "GPX Route Builder" \
    -fill "#457b9d" -font Helvetica -pointsize 17 \
    -gravity Center -annotate +0+20 "Trascina l'icona nella cartella Applicazioni per installare" \
    -fill "#888888" -font Helvetica -pointsize 13 \
    -gravity South -annotate +0+30 "albertoocello/gpx-route-builder" \
    "$BUILD/dmg_background.png" 2>/dev/null || \
magick -size 800x450 xc:"#dde8f0" \
    -fill "#1d3557" -pointsize 22 \
    -gravity Center -annotate +0+0 "Trascina GPX Route Builder in Applicazioni" \
    "$BUILD/dmg_background.png"
echo "   sfondo creato"

echo "==> Creazione .dmg"
rm -f "$DMG_OUT"
create-dmg \
    --volname "GPX Route Builder" \
    --volicon "$BUILD/AppIcon.icns" \
    --window-pos 200 140 \
    --window-size 800 450 \
    --icon-size 128 \
    --icon "GPX Route Builder.app" 200 220 \
    --hide-extension "GPX Route Builder.app" \
    --app-drop-link 600 220 \
    --background "$BUILD/dmg_background.png" \
    --codesign "$SIGN_ID" \
    "$DMG_OUT" \
    "$BUILD/" 2>&1 | grep -E "(Created|Error|hdiutil|codesign)" || true

echo ""
echo "✅  $DMG_OUT"
ls -lh "$DMG_OUT"
