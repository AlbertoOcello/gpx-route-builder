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
python3 - "$BUILD/dmg_background.png" <<'PYEOF'
import sys, struct, zlib, os

# ---------- minimal PNG writer (no Pillow dependency) ----------
def _chunk(tag, data):
    c = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", c)

def write_png(path, w, h, pixels):
    raw = b""
    for y in range(h):
        raw += b"\x00"
        for x in range(w):
            raw += bytes(pixels[y][x])
    compressed = zlib.compress(raw, 6)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(_chunk(b"IDAT", compressed))
        f.write(_chunk(b"IEND", b""))

W, H = 800, 450

# gradient background: #e8eef5 → #cddaea  (top → bottom)
top = (0xe8, 0xee, 0xf5)
bot = (0xcd, 0xda, 0xea)

pixels = []
for y in range(H):
    t = y / (H - 1)
    r = int(top[0] + (bot[0] - top[0]) * t)
    g = int(top[1] + (bot[1] - top[1]) * t)
    b = int(top[2] + (bot[2] - top[2]) * t)
    pixels.append([(r, g, b)] * W)

# ---------- blit text via Pillow if available, else skip ----------
try:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (W, H))
    img.putdata([tuple(pixels[y][x]) for y in range(H) for x in range(W)])
    draw = ImageDraw.Draw(img)

    def load_font(size, bold=False):
        candidates = [
            f"/System/Library/Fonts/{'SFNSDisplay-Bold' if bold else 'SFNSDisplay'}.otf",
            f"/System/Library/Fonts/Supplemental/{'Arial Bold' if bold else 'Arial'}.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Geneva.ttf",
        ]
        for p in candidates:
            if os.path.exists(p):
                try: return ImageFont.truetype(p, size)
                except: pass
        return ImageFont.load_default()

    f_title  = load_font(32, bold=True)
    f_body   = load_font(18)
    f_small  = load_font(13)

    def centered_x(text, font):
        bb = draw.textbbox((0, 0), text, font=font)
        return (W - (bb[2] - bb[0])) // 2

    title = "GPX Route Builder"
    body  = "Trascina l'icona nella cartella Applicazioni per installare"
    sub   = "albertoocello/gpx-route-builder  •  v1.0"

    draw.text((centered_x(title, f_title), 70),  title, fill="#1d3557", font=f_title)
    draw.text((centered_x(body,  f_body),  210), body,  fill="#457b9d", font=f_body)
    draw.text((centered_x(sub,   f_small), 410), sub,   fill="#888888", font=f_small)

    img.save(sys.argv[1])
except ImportError:
    # Pillow not available: write gradient-only PNG
    write_png(sys.argv[1], W, H, pixels)

print("   sfondo creato")
PYEOF

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
