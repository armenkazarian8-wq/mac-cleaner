#!/bin/bash
# Builds "Mac Cleaner.app", zips it to dist/MacCleaner.zip, and installs it into /Applications
# (or ~/Applications if that isn't writable). Pass --no-install to only build and zip.
set -euo pipefail
cd "$(dirname "$0")"
BUILD=build
APP="$BUILD/Mac Cleaner.app"
rm -rf "$BUILD" && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "› Compiling app"
# Universal binary (Apple silicon + Intel) that runs on macOS 13 and later.
for arch in arm64 x86_64; do
  swiftc -O -swift-version 5 -target $arch-apple-macos13.0 -o "$BUILD/MacCleaner-$arch" main.swift -framework Cocoa -framework WebKit
done
lipo -create -output "$APP/Contents/MacOS/MacCleaner" "$BUILD/MacCleaner-arm64" "$BUILD/MacCleaner-x86_64"

echo "› Drawing icon"
swiftc -O -swift-version 5 -o "$BUILD/icon" icon.swift -framework Cocoa
"$BUILD/icon" "$BUILD/icon.png"
ICONSET="$BUILD/AppIcon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

cp cleaner.py ui.html "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Mac Cleaner</string>
  <key>CFBundleDisplayName</key><string>Mac Cleaner</string>
  <key>CFBundleIdentifier</key><string>com.armen.maccleaner</string>
  <key>CFBundleExecutable</key><string>MacCleaner</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>2.0</string>
  <key>CFBundleVersion</key><string>2</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Open source — github.com/armenkazarian8-wq/mac-cleaner</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSAppleEventsUsageDescription</key><string>Mac Cleaner asks Finder to move protected items to the Trash.</string>
</dict></plist>
PLIST

echo "› Signing"
codesign --force --deep --sign - "$APP"

echo "› Packaging dist/MacCleaner.zip"
mkdir -p dist && rm -f dist/MacCleaner.zip
ditto -c -k --sequesterRsrc --keepParent "$APP" dist/MacCleaner.zip

[ "${1:-}" = "--no-install" ] && { echo "✓ Built $APP and dist/MacCleaner.zip"; exit 0; }
DEST=/Applications
[ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
pkill -x MacCleaner 2>/dev/null || true
rm -rf "$DEST/Mac Cleaner.app"
cp -R "$APP" "$DEST/"
echo "✓ Installed to $DEST/Mac Cleaner.app"
