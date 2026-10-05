#!/bin/zsh
set -euo pipefail
ROOT=${0:A:h:h}
cd "$ROOT/app"
# One app for Apple silicon and Intel Macs: build each architecture, then join them (works without full Xcode).
for ARCH in arm64 x86_64; do swift build -c release --triple "$ARCH-apple-macosx13.0"; done
BIN=$(mktemp -d)
lipo -create -output "$BIN/AGYRotator" \
  "$(swift build -c release --triple arm64-apple-macosx13.0 --show-bin-path)/AGYRotator" \
  "$(swift build -c release --triple x86_64-apple-macosx13.0 --show-bin-path)/AGYRotator"
APP="$ROOT/dist/Account Rotator.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ROOT/app/Resources/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$BIN/AGYRotator" "$APP/Contents/MacOS/AGYRotator"
# The rotator itself (daemon, helpers, console) travels inside the app; the app registers it as login services.
PAYLOAD="$APP/Contents/Resources/rotator"
rm -rf "$PAYLOAD" && mkdir -p "$PAYLOAD"
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude 'test_*.py' \
  "$ROOT/agy-rotator" "$ROOT/daemon" "$ROOT/helpers" "$ROOT/web" "$PAYLOAD/"
# Build stamp: when it changes, the app replaces the installed copy and restarts the services.
echo "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo src)-$(date +%Y%m%d%H%M%S)" > "$PAYLOAD/BUILD"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>io.account-rotator.app</string>
<key>CFBundleName</key><string>Account Rotator</string>
<key>CFBundleDisplayName</key><string>Account Rotator</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundleExecutable</key><string>AGYRotator</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.1</string>
<key>CFBundleVersion</key><string>2</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
/usr/bin/codesign --force --sign - "$APP"
/usr/bin/plutil -lint "$APP/Contents/Info.plist"
echo "$APP"
